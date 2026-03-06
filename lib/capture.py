"""
Layer 1 -- Four-tier page capture with article extraction.

Depends on: lib.paths (Layer 0).
External deps: requests, readability-lxml, beautifulsoup4, lxml.

Provides a unified capture_page() function that tries four methods
in order of speed, stealth, and reliability:
  Tier 1: HTTP GET + readability-lxml  (~2s, works for ~60-70% of articles)
  Tier 2: Chrome CDP (JS rendering)    (~4-5s, SPAs + JS-heavy sites)
  Tier 3: Chrome CLI --dump-dom        (~10s, backup JS renderer)
  Tier 4: Wayback Machine CDX API      (~5s, archival safety net)

All tiers return the same dict shape for uniform handling downstream.
"""

import json
import re
import subprocess
import tempfile
import shutil
import hashlib
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

from lib.paths import ARCHIVES_DIR

# -- Constants --

MIN_TEXT_SIZE = 200  # chars -- below this, capture is considered failed
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

# Signals that indicate bot protection or access restriction
BOT_BLOCK_SIGNALS = [
    "access denied", "403 forbidden", "just a moment", "checking your browser",
    "enable javascript", "captcha", "cloudflare", "are you a robot",
    "access restricted", "blocked", "security check",
]

# Signals that indicate a paywall
PAYWALL_SIGNALS = [
    "subscribe now", "subscriber to access", "sign in to read",
    "subscription required", "premium content", "paywall",
    "already a subscriber", "this content is only available",
]


# -- Public API --

def capture_page(url, timeout=15, start_tier=1):
    """Four-tier page capture with article extraction.

    Tries tiers in order. Returns first successful capture.
    Each tier extracts article text via readability-lxml for consistency.

    Args:
        url: The URL to capture
        timeout: Per-tier timeout in seconds (default 15)
        start_tier: Start from this tier (default 1). Use 2 or 3 to skip
                    earlier tiers (e.g., for SPA escalation after homepage-redirect).

    Returns:
        dict with keys:
            success (bool): Whether any tier produced sufficient content
            html (str): Raw HTML (may be empty on failure)
            article_text (str): Clean article text from readability
            title (str): Article title
            metadata (dict): author, published_date, description, site_name
            capture_method (str): "http-readability", "chrome-cli", "wayback", or "none"
            html_size (int): Length of raw HTML
            text_size (int): Length of extracted article text
            error (str|None): Error message if all tiers failed
            tier_errors (dict): Per-tier error messages for diagnostics
    """
    tier_errors = {}

    # Tier 1: HTTP + readability (fast, undetectable)
    if start_tier <= 1:
        result = _tier1_http_readability(url, timeout=timeout)
        if result["success"]:
            result["tier_errors"] = tier_errors
            return result
        tier_errors["tier1"] = result.get("error", "unknown")

        # Short-circuit: if Tier 1 detected a PDF, don't waste time on other tiers
        if "pdf-detected" in result.get("error", ""):
            result["tier_errors"] = tier_errors
            return result

        # Short-circuit: if Tier 1 got an auth/paywall HTTP error, JS rendering
        # can't bypass authentication -- skip Tiers 2-3 and go straight to Wayback
        t1_error = result.get("error", "")
        if _is_auth_blocked(t1_error):
            # Skip JS tiers, try Wayback as last resort (may have a pre-paywall snapshot)
            tier_errors["tier2"] = "skipped: auth-blocked"
            tier_errors["tier3"] = "skipped: auth-blocked"
            result = _tier4_wayback(url, timeout=timeout)
            if result["success"]:
                result["tier_errors"] = tier_errors
                return result
            tier_errors["tier4"] = result.get("error", "unknown")
            return {
                "success": False, "html": "", "article_text": "", "title": "",
                "metadata": {}, "capture_method": "none", "html_size": 0,
                "text_size": 0, "tier_errors": tier_errors,
                "error": f"Auth-blocked (fast exit): {t1_error}. Wayback: {tier_errors.get('tier4', 'n/a')}",
            }

    # Tier 2: nodriver (anti-detection headless browser)
    if start_tier <= 2:
        result = _tier2_nodriver(url, timeout=max(timeout, 20))
        if result["success"]:
            result["tier_errors"] = tier_errors
            return result
        tier_errors["tier2"] = result.get("error", "unknown")

    # Tier 3: Chrome CLI --dump-dom (backup JS renderer)
    if start_tier <= 3:
        result = _tier3_chrome_cli(url, timeout=max(timeout, 30))
        if result["success"]:
            result["tier_errors"] = tier_errors
            return result
        tier_errors["tier3"] = result.get("error", "unknown")

    # Tier 4: Wayback Machine CDX (archival safety net)
    result = _tier4_wayback(url, timeout=timeout)
    if result["success"]:
        result["tier_errors"] = tier_errors
        return result
    tier_errors["tier4"] = result.get("error", "unknown")

    # All tiers failed
    return {
        "success": False,
        "html": "",
        "article_text": "",
        "title": "",
        "metadata": {},
        "capture_method": "none",
        "html_size": 0,
        "text_size": 0,
        "error": f"All capture tiers failed: {tier_errors}",
        "tier_errors": tier_errors,
    }


# -- Tier 1: HTTP + readability --

def _tier1_http_readability(url, timeout=15):
    """Fetch page via requests.get() and extract article via readability-lxml.

    Fast (~2s), undetectable (looks like a normal browser request).
    Works for most news articles, blogs, and static HTML pages.
    Fails on JS-rendered SPAs, bot-protected sites, and paywalled content.
    """
    import requests as req_lib

    try:
        resp = req_lib.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        resp.raise_for_status()

        # Detect PDF Content-Type -- fail fast with helpful message
        content_type = resp.headers.get("Content-Type", "").lower()
        if "application/pdf" in content_type or url.lower().rstrip("/").endswith(".pdf"):
            return _fail(
                "pdf-detected: Use research('search_pdf', {'path': '<url>', 'search_terms': '...'}) for PDF sources",
                "http-readability"
            )

        resp.encoding = resp.apparent_encoding or "utf-8"
        html = resp.text

        if len(html) < 100:
            return _fail("page-too-small", "http-readability")

        # Check for bot blocks before extraction
        html_lower = html[:5000].lower()
        for signal in BOT_BLOCK_SIGNALS:
            if signal in html_lower and len(html) < 5000:
                return _fail(f"bot-blocked:{signal}", "http-readability")

        # Extract article via readability
        article_text, title = _readability_extract(html)

        if not article_text or len(article_text) < MIN_TEXT_SIZE:
            return _fail("insufficient-content", "http-readability")

        # Check for paywall signals in extracted text
        text_lower = article_text.lower()
        for signal in PAYWALL_SIGNALS:
            if signal in text_lower and len(article_text) < 500:
                return _fail(f"paywall:{signal}", "http-readability")

        metadata = _extract_metadata(html)

        return {
            "success": True,
            "html": html,
            "article_text": article_text,
            "title": title,
            "metadata": metadata,
            "capture_method": "http-readability",
            "html_size": len(html),
            "text_size": len(article_text),
            "error": None,
        }

    except req_lib.exceptions.Timeout:
        return _fail("timeout", "http-readability")
    except req_lib.exceptions.ConnectionError as e:
        return _fail(f"connection-error: {str(e)[:80]}", "http-readability")
    except req_lib.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        return _fail(f"http-{code}", "http-readability")
    except Exception as e:
        return _fail(f"error: {str(e)[:80]}", "http-readability")


# -- Tier 2: nodriver (anti-detection) --

def _tier2_nodriver(url, timeout=20):
    """Capture page via Chrome CDP in a subprocess.

    Uses direct Chrome DevTools Protocol for JS-rendered content.
    Runs as a subprocess to isolate the Chrome process lifecycle.
    Handles JS-rendered SPAs that Tier 1's HTTP fetch can't capture.
    """
    nodriver_script = Path(__file__).parent / "nodriver_capture.py"
    python_exe = r"C:\Python313\python.exe"

    if not nodriver_script.exists():
        return _fail("nodriver-script-missing", "nodriver")

    try:
        # Use a temp file for result -- nodriver pollutes stdout with cleanup messages
        result_file = Path(tempfile.mktemp(suffix=".json", prefix="nd_capture_"))

        params_json = json.dumps({
            "url": url,
            "wait_seconds": 3,
            "result_file": str(result_file),
        })
        try:
            proc = subprocess.run(
                [python_exe, str(nodriver_script)],
                input=params_json,
                capture_output=True,
                timeout=timeout,
                encoding="utf-8",
                errors="replace",
            )

            if not result_file.exists():
                stdout_hint = proc.stdout[:200] if proc.stdout else "no output"
                stderr_hint = proc.stderr[:200] if proc.stderr else ""
                return _fail(
                    f"nodriver-no-result (exit={proc.returncode}): {stdout_hint} {stderr_hint}".strip(),
                    "nodriver"
                )

            try:
                nd_result = json.loads(result_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                return _fail(f"nodriver-bad-json: {e}", "nodriver")
        finally:
            # Clean up temp file
            try:
                result_file.unlink(missing_ok=True)
            except Exception:
                pass

        if not nd_result.get("success"):
            return _fail(f"nodriver-failed: {nd_result.get('error', 'unknown')}", "nodriver")

        html = nd_result.get("html", "")
        text = nd_result.get("text", "")
        title = nd_result.get("title", "")

        if not html or len(html) < 100:
            return _fail("nodriver-empty", "nodriver")

        # Check for bot blocks even in nodriver output
        html_lower = html[:5000].lower()
        for signal in BOT_BLOCK_SIGNALS:
            if signal in html_lower and len(html) < 5000:
                return _fail(f"bot-blocked:{signal}", "nodriver")

        # Extract article via readability (same as other tiers)
        article_text, rd_title = _readability_extract(html)

        if not article_text or len(article_text) < MIN_TEXT_SIZE:
            # Fall back to innerText from nodriver if readability fails
            if text and len(text) >= MIN_TEXT_SIZE:
                article_text = text
                if not rd_title:
                    rd_title = title
            else:
                return _fail("insufficient-content", "nodriver")

        if rd_title:
            title = rd_title

        metadata = _extract_metadata(html)

        return {
            "success": True,
            "html": html,
            "article_text": article_text,
            "title": title,
            "metadata": metadata,
            "capture_method": "nodriver",
            "html_size": len(html),
            "text_size": len(article_text),
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return _fail(f"nodriver-timeout ({timeout}s)", "nodriver")
    except FileNotFoundError:
        return _fail("python-not-found", "nodriver")
    except Exception as e:
        return _fail(f"nodriver-error: {str(e)[:80]}", "nodriver")


# -- Tier 3: Chrome CLI --

def _tier3_chrome_cli(url, timeout=30):
    """Capture page using Chrome --headless=new --dump-dom.

    Renders JavaScript, so works for SPAs and JS-heavy sites.
    Slower (~10s) and more detectable than Tier 1.
    """
    temp_profile = tempfile.mkdtemp(prefix="capture_tier2_")
    wait_ms = 5000  # 5s virtual time budget for JS rendering

    try:
        result = subprocess.run(
            [CHROME_PATH,
             "--headless=new", "--dump-dom",
             f"--virtual-time-budget={wait_ms}",
             "--no-first-run", "--no-default-browser-check",
             "--disable-gpu", "--disable-extensions",
             "--disable-background-networking",
             f"--user-data-dir={temp_profile}",
             url],
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

        html = result.stdout
        if not html or len(html) < 100:
            return _fail(
                f"chrome-empty (exit={result.returncode}, stderr={result.stderr[:100]})",
                "chrome-cli"
            )

        # Check for bot blocks
        html_lower = html[:5000].lower()
        for signal in BOT_BLOCK_SIGNALS:
            if signal in html_lower and len(html) < 5000:
                return _fail(f"bot-blocked:{signal}", "chrome-cli")

        # Extract with readability
        article_text, title = _readability_extract(html)

        if not article_text or len(article_text) < MIN_TEXT_SIZE:
            # Fall back to raw text extraction
            raw_text = _html_to_text(html)
            if len(raw_text) >= MIN_TEXT_SIZE:
                article_text = raw_text
                if not title:
                    title_match = re.search(r'<title[^>]*>(.*?)</title>', html, re.IGNORECASE | re.DOTALL)
                    title = title_match.group(1).strip() if title_match else ""
            else:
                return _fail("insufficient-content", "chrome-cli")

        metadata = _extract_metadata(html)

        return {
            "success": True,
            "html": html,
            "article_text": article_text,
            "title": title,
            "metadata": metadata,
            "capture_method": "chrome-cli",
            "html_size": len(html),
            "text_size": len(article_text),
            "error": None,
        }

    except subprocess.TimeoutExpired:
        return _fail(f"chrome-timeout ({timeout}s)", "chrome-cli")
    except FileNotFoundError:
        return _fail("chrome-not-found", "chrome-cli")
    except Exception as e:
        return _fail(f"chrome-error: {str(e)[:80]}", "chrome-cli")
    finally:
        shutil.rmtree(temp_profile, ignore_errors=True)


# -- Tier 4: Wayback Machine --

def _tier4_wayback(url, timeout=15):
    """Fetch page from Wayback Machine CDX API.

    Safety net for pages that are bot-blocked or offline.
    Only works if the page was previously archived by someone.
    """
    try:
        # Step 1: Check CDX API for available snapshots
        cdx_url = f"http://archive.org/wayback/available?url={urllib.parse.quote(url, safe='')}"
        req = urllib.request.Request(cdx_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cdx_data = json.loads(resp.read().decode("utf-8"))

        snapshot = cdx_data.get("archived_snapshots", {}).get("closest", {})
        if not snapshot.get("available"):
            return _fail("not-in-wayback", "wayback")

        wayback_url = snapshot.get("url", "")
        timestamp = snapshot.get("timestamp", "")

        if not wayback_url:
            return _fail("no-wayback-url", "wayback")

        # Step 2: Fetch the snapshot via requests (cleaner than urllib)
        import requests as req_lib
        wb_resp = req_lib.get(
            wayback_url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        wb_resp.raise_for_status()
        wb_resp.encoding = wb_resp.apparent_encoding or "utf-8"
        html = wb_resp.text

        if len(html) < 200:
            return _fail("wayback-page-too-small", "wayback")

        # Step 3: Extract article
        article_text, title = _readability_extract(html)

        if not article_text or len(article_text) < MIN_TEXT_SIZE:
            raw_text = _html_to_text(html)
            if len(raw_text) >= MIN_TEXT_SIZE:
                article_text = raw_text
            else:
                return _fail("wayback-insufficient-content", "wayback")

        metadata = _extract_metadata(html)
        metadata["wayback_timestamp"] = timestamp
        metadata["wayback_url"] = wayback_url

        return {
            "success": True,
            "html": html,
            "article_text": article_text,
            "title": title,
            "metadata": metadata,
            "capture_method": "wayback",
            "html_size": len(html),
            "text_size": len(article_text),
            "error": None,
            "wayback_url": wayback_url,
        }

    except urllib.error.URLError as e:
        return _fail(f"wayback-network: {str(e)[:80]}", "wayback")
    except Exception as e:
        return _fail(f"wayback-error: {str(e)[:80]}", "wayback")


# -- Shared Extraction Functions --

def _readability_extract(html):
    """Extract article text and title using readability-lxml + BeautifulSoup.

    Returns:
        (article_text: str, title: str)
    """
    try:
        from readability import Document
        from bs4 import BeautifulSoup

        doc = Document(html)
        article_html = doc.summary()
        title = doc.short_title() or ""

        # Convert article HTML to clean text via BeautifulSoup
        soup = BeautifulSoup(article_html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)

        # Clean up whitespace
        lines = [line.strip() for line in text.splitlines()]
        article_text = "\n".join(line for line in lines if line)

        return article_text, title

    except Exception:
        return "", ""


def _html_to_text(html):
    """Fallback text extraction via regex when readability fails."""
    import html as html_module

    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'<style[^>]*>.*?</style>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'<noscript[^>]*>.*?</noscript>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'<(?:br|p|div|h[1-6]|li|tr|blockquote|hr)[^>]*/?>', '\n', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    text = html_module.unescape(cleaned)
    lines = [line.strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def _extract_metadata(html):
    """Extract article metadata from HTML meta tags via regex.

    Returns:
        dict with author, published_date, description, site_name (any may be None)
    """
    def _get_meta(names):
        for name in names:
            # property="name" content="value"
            match = re.search(
                rf'<meta\s+(?:property|name)=["\'](?:{re.escape(name)})["\']\s+content=["\']([^"\']*)["\']',
                html, re.IGNORECASE
            )
            if not match:
                # content="value" property="name"
                match = re.search(
                    rf'<meta\s+content=["\']([^"\']*)["\']\s+(?:property|name)=["\'](?:{re.escape(name)})["\']',
                    html, re.IGNORECASE
                )
            if match:
                return match.group(1)
        return None

    return {
        "author": _get_meta(["author", "article:author", "og:article:author"]),
        "published_date": _get_meta(["article:published_time", "datePublished",
                                      "og:article:published_time", "date"]),
        "description": _get_meta(["og:description", "description"]),
        "site_name": _get_meta(["og:site_name"]),
    }


# -- File Persistence --

def save_capture(url, capture_result, archives_dir=None):
    """Save capture result to the archive filesystem.

    Creates HTML, text, and metadata files in the standard archive layout.

    Args:
        url: The original URL
        capture_result: dict from capture_page()
        archives_dir: Override base directory (default: ARCHIVES_DIR)

    Returns:
        dict with html_path, text_path, meta_path, article_text_path
    """
    base = Path(archives_dir) if archives_dir else ARCHIVES_DIR
    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    safe_domain = domain.replace(":", "_").replace("/", "_")
    domain_dir = base / safe_domain
    domain_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
    base_name = f"{date_str}_{url_hash}"

    html_path = domain_dir / f"{base_name}.html"
    text_path = domain_dir / f"{base_name}.txt"
    article_path = domain_dir / f"{base_name}_article.txt"
    meta_path = domain_dir / f"{base_name}.meta.json"

    # Save HTML
    if capture_result.get("html"):
        html_path.write_text(capture_result["html"], encoding="utf-8")

    # Save article text with header
    title = capture_result.get("title", "Untitled")
    article_text = capture_result.get("article_text", "")
    metadata = capture_result.get("metadata", {})

    header = (
        f"{title}\n"
        f"URL: {url}\n"
        f"Captured: {date_str} via {capture_result.get('capture_method', 'unknown')}\n"
        f"{'=' * 80}\n\n"
    )
    article_path.write_text(header + article_text, encoding="utf-8")

    # Save raw text (stripped HTML)
    raw_text = _html_to_text(capture_result.get("html", "")) if capture_result.get("html") else article_text
    text_path.write_text(raw_text, encoding="utf-8")

    # Save metadata
    meta = {
        "url": url,
        "domain": domain,
        "title": title,
        "captured_at": now.isoformat(),
        "url_hash": url_hash,
        "capture_method": capture_result.get("capture_method"),
        "html_size": capture_result.get("html_size", 0),
        "text_size": capture_result.get("text_size", 0),
        "word_count": len(article_text.split()),
        "author": metadata.get("author"),
        "published_date": metadata.get("published_date"),
        "site_name": metadata.get("site_name"),
        "text_preview": article_text[:500],
        "tier_errors": capture_result.get("tier_errors", {}),
    }
    if capture_result.get("wayback_url"):
        meta["wayback_url"] = capture_result["wayback_url"]

    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    return {
        "html_path": str(html_path),
        "text_path": str(text_path),
        "article_text_path": str(article_path),
        "meta_path": str(meta_path),
    }


def _is_auth_blocked(error_str):
    """Check if a Tier 1 error indicates an auth/paywall block.

    These errors cannot be resolved by JS rendering (Tiers 2-3),
    so we skip straight to Wayback (Tier 4) to save 30-40 seconds.

    Matches: http-401, http-403, paywall:* signals.
    Does NOT match: http-404 (could be a routing issue that JS rendering fixes),
    bot-blocked (some bot blocks are JS-challenge-based and Tier 2-3 can pass them).
    """
    if not error_str:
        return False
    return (
        error_str.startswith("http-401")
        or error_str.startswith("http-403")
        or error_str.startswith("paywall:")
    )


def _fail(error, method):
    """Build a standardized failure result."""
    return {
        "success": False,
        "html": "",
        "article_text": "",
        "title": "",
        "metadata": {},
        "capture_method": method,
        "html_size": 0,
        "text_size": 0,
        "error": error,
    }
