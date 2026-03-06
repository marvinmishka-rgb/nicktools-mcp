"""Unified web reader: HTTP + Chrome + Wayback with optional anti-detection.
---
description: Read a URL with four-tier capture, optional stealth mode, and optional archiving
creates_nodes: [Source]
creates_edges: []
databases: [corcoran, lifestream]
---

Primary tool for reading web pages. Replaces fetch_page and browse_url with a
single intent-based operation.

Default path: four-tier capture pipeline (HTTP+readability -> nodriver -> Chrome CLI
-> Wayback) via lib/capture.py. Handles junk detection, SPA escalation, and rate
limiting. Optionally archives with Source node creation.

Stealth path (stealth=true): anti-detection browsing via nodriver subprocess with
response caching and rate limiting. Use for bot-protected sites that block the
default HTTP tier.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.io import setup_output, load_params, output
from lib.urls import canonicalize_url, extract_domain, SOURCE_TYPE_MAP
from lib.capture import capture_page, save_capture, MIN_TEXT_SIZE
from lib.browsing import enforce_rate_limit, record_request, BROWSE_RATE_FILE
from lib.spn import enqueue_spn
from lib.paths import ARCHIVES_DIR
from lib.db import GRAPH_DATABASE, ENTRY_DATABASE


# ============================================================
# Junk content detection
# ============================================================

BOT_BLOCK_TITLE_PATTERNS = re.compile(
    r"just a moment|human verification|security check|"
    r"checking your browser|access denied|attention required|"
    r"verify you are human|captcha|challenge|"
    r"are you a robot|bot detection|ddos protection|"
    r"please wait|one more step|performing security",
    re.IGNORECASE,
)

BOT_BLOCK_CONTENT_PATTERNS = re.compile(
    r"(you need to solve a puzzle|"
    r"confirm you are human|"
    r"security check before continuing|"
    r"verifies that you are not a bot|"
    r"performing security verification|"
    r"enable javascript and cookies|"
    r"checking if the site connection is secure|"
    r"waiting for .+ to respond)",
    re.IGNORECASE,
)


def _detect_junk_content(title, content, text_size, url):
    """Detect if captured content is junk (CAPTCHA, bot block, homepage redirect).

    Returns:
        (is_junk: bool, reason: str or None)
    """
    title_lower = (title or "").lower().strip()

    # 1. Bot-block / CAPTCHA title
    if title and BOT_BLOCK_TITLE_PATTERNS.search(title):
        return True, f"bot-blocked: title indicates CAPTCHA/verification page ('{title[:60]}')"

    # 2. Bot-block content patterns
    if content and BOT_BLOCK_CONTENT_PATTERNS.search(content):
        return True, "bot-blocked: content contains CAPTCHA/verification signals"

    # 3. Soft 404 detection
    if title_lower and ("not found" in title_lower or "404" in title_lower):
        return True, f"soft-404: title indicates page not found ('{title[:60]}')"

    # 4. Homepage redirect detection
    url_has_path = len(url.split("/")) > 4
    if url_has_path and text_size < 500:
        if title_lower and not any(c in title_lower for c in [":", "-", "|", "\u2014"]):
            return True, f"homepage-redirect: URL has article path but got generic page ({text_size} chars, title='{title[:60]}')"

    return False, None


# ============================================================
# Stealth path: subprocess delegation to browse_url
# ============================================================

def _read_stealth(url, extract="text", js_eval="", wait_seconds=3,
                  bypass_cache=False, cache_ttl=3600, min_delay=None,
                  max_retries=3, timeout=60):
    """Read a URL using anti-detection nodriver browser (subprocess).

    Delegates to browse_url.py which handles:
    - nodriver with bot-evasion techniques
    - Per-domain rate limiting and response caching
    - JavaScript evaluation
    - Multiple extract modes (text, html, links, all)

    Returns dict in read's standard output shape.
    """
    browse_script = Path(__file__).parent / "browse_url.py"
    python_exe = r"C:\Python313\python.exe"

    params = {
        "url": url,
        "extract": extract,
        "wait_seconds": wait_seconds,
        "bypass_cache": bypass_cache,
        "cache_ttl": cache_ttl,
        "max_retries": max_retries,
    }
    if js_eval:
        params["js_eval"] = js_eval
    if min_delay is not None:
        params["min_delay"] = min_delay

    # Write params to temp file (same pattern as server.py subprocess dispatch)
    params_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='_browse_params.json', delete=False,
        encoding='utf-8'
    )
    json.dump(params, params_file, ensure_ascii=False)
    params_file.close()

    try:
        proc = subprocess.run(
            [python_exe, str(browse_script), params_file.name],
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

        stdout = (proc.stdout or "").strip()
        stderr_text = (proc.stderr or "").strip()

        if proc.returncode != 0:
            stderr_hint = stderr_text[:300]
            return {
                "success": False,
                "error": f"Stealth browse failed (exit {proc.returncode}): {stderr_hint}",
                "capture_method": "stealth-nodriver",
            }

        # Parse JSON output from browse_url.
        # browse_url may emit multiple JSON objects (rate limit notices, then result)
        # plus nodriver cleanup messages on stdout. Extract last valid JSON object.
        if not stdout:
            return {
                "success": False,
                "error": "Stealth browse returned empty output",
                "capture_method": "stealth-nodriver",
            }

        browse_result = None
        # Use raw_decode to extract JSON objects from stdout.
        # Take the last valid dict (the actual result, not rate-limit notices).
        decoder = json.JSONDecoder()
        pos = 0
        while pos < len(stdout):
            # Skip whitespace and non-JSON text
            while pos < len(stdout) and stdout[pos] != '{':
                pos += 1
            if pos >= len(stdout):
                break
            try:
                obj, end_pos = decoder.raw_decode(stdout, pos)
                if isinstance(obj, dict):
                    browse_result = obj  # keep last dict
                pos = end_pos
            except json.JSONDecodeError:
                pos += 1  # skip this '{' and try next

        if browse_result is None:
            return {
                "success": False,
                "error": f"Stealth browse output not valid JSON. First 300 chars: {stdout[:300]}",
                "capture_method": "stealth-nodriver",
            }

        # Normalize browse_url output to read's return shape
        text = browse_result.get("text", "")
        return {
            "success": bool(text and len(text.strip()) > 50),
            "url": browse_result.get("url", url),
            "domain": browse_result.get("domain", extract_domain(url)),
            "title": browse_result.get("title", ""),
            "content": text if extract == "text" else browse_result.get(extract, text),
            "text_size": len(text),
            "word_count": len(text.split()) if text else 0,
            "capture_method": "stealth-nodriver",
            "metadata": {
                "from_cache": browse_result.get("_from_cache", False),
                "cache_age": browse_result.get("_cache_age_seconds"),
                "attempt": browse_result.get("_attempt"),
            },
            "links": browse_result.get("links", []) if extract in ("links", "all") else [],
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"Stealth browse timed out after {timeout}s",
            "capture_method": "stealth-nodriver",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Stealth browse error: {e}",
            "capture_method": "stealth-nodriver",
        }
    finally:
        try:
            os.unlink(params_file.name)
        except Exception:
            pass


# ============================================================
# Source node creation (shared with archive tool)
# ============================================================

def _create_source_node(driver, url, domain, capture, paths, tags):
    """Create Source node in both corcoran and lifestream databases."""
    now = datetime.now(timezone.utc)
    article_text = capture.get("article_text", "")
    metadata = capture.get("metadata", {})
    source_type = SOURCE_TYPE_MAP.get(domain, SOURCE_TYPE_MAP.get("_default", "article"))

    params = {
        "url": url,
        "domain": domain,
        "title": capture.get("title", "Untitled"),
        "captured": now.isoformat(),
        "archive_status": "captured",
        "capture_status": "captured",
        "capture_method": capture.get("capture_method", "unknown"),
        "text_size": len(article_text),
        "text_preview": article_text[:500],
        "text_path": paths.get("article_text_path", paths.get("text_path", "")),
        "html_path": paths.get("html_path", ""),
        "source_type": source_type,
        "author": metadata.get("author"),
        "published_date": metadata.get("published_date"),
        "site_name": metadata.get("site_name"),
        "tags": tags or [],
    }

    source_cypher = """
        MERGE (s:Source {url: $url})
        ON CREATE SET
            s.domain = $domain, s.title = $title,
            s.capturedAt = datetime($captured), s.lastCaptured = datetime($captured),
            s.captureCount = 1, s.archiveStatus = $archive_status, s.captureStatus = $capture_status,
            s.captureMethod = $capture_method, s.textSize = $text_size,
            s.textPreview = $text_preview, s.archivePath = $text_path,
            s.htmlPath = $html_path, s.sourceType = $source_type,
            s.author = $author, s.publishedDate = $published_date,
            s.siteName = $site_name, s.tags = $tags
        ON MATCH SET
            s.lastCaptured = datetime($captured),
            s.captureCount = COALESCE(s.captureCount, 0) + 1,
            s.title = CASE WHEN $title <> 'Untitled' THEN $title ELSE s.title END,
            s.archiveStatus = $archive_status, s.captureStatus = $capture_status, s.captureMethod = $capture_method,
            s.textSize = $text_size, s.textPreview = $text_preview,
            s.archivePath = $text_path, s.htmlPath = $html_path,
            s.sourceType = COALESCE($source_type, s.sourceType),
            s.author = COALESCE($author, s.author),
            s.publishedDate = COALESCE($published_date, s.publishedDate),
            s.siteName = COALESCE($site_name, s.siteName),
            s.tags = CASE WHEN s.tags IS NULL THEN $tags
                     ELSE [x IN s.tags WHERE NOT x IN $tags] + $tags END
    """

    for db_name in [GRAPH_DATABASE, ENTRY_DATABASE]:
        with driver.session(database=db_name) as session:
            session.run(source_cypher, params)


# ============================================================
# Main read implementation
# ============================================================

def read_impl(url, format="text", archive=False, spn=True, tags=None,
              min_text_size=None, timeout=15, stealth=False, extract="text",
              js_eval="", bypass_cache=False, cache_ttl=3600, min_delay=None,
              max_retries=3, driver=None, **kwargs):
    """Read a web page with optional stealth mode and archiving.

    Default path: four-tier capture pipeline (HTTP+readability -> nodriver ->
    Chrome CLI -> Wayback). Handles junk detection, SPA escalation, rate limiting.

    Stealth path (stealth=true): anti-detection nodriver browser with response
    caching and rate limiting. Use for bot-protected sites.

    Args:
        url: URL to read (required)
        format: Output format -- "text" (default), "html", or "full"
        archive: If true, create Source node and save files (default False)
        spn: Queue URL for Wayback Machine SPN on success (default True)
        tags: Tags for Source node if archive=True (list or CSV string)
        min_text_size: Minimum chars for successful extraction (default from lib/capture)
        timeout: Per-tier timeout in seconds (default 15)
        stealth: Use anti-detection nodriver browser (default False)
        extract: Extract mode for stealth -- "text", "html", "links", "all" (default "text")
        js_eval: JavaScript to evaluate on page (stealth mode only)
        bypass_cache: Skip response cache (stealth mode only)
        cache_ttl: Cache TTL in seconds (stealth mode, default 3600)
        min_delay: Override domain rate limit delay in seconds (stealth mode)
        max_retries: Max retry attempts for stealth mode (default 3)
        driver: Neo4j driver (injected by dispatcher)

    Returns:
        dict with success, content, title, url, domain, metadata,
        capture_method, text_size, word_count, archive_status
    """
    if not url:
        return {"error": "Missing required parameter 'url'"}

    # --- Stealth path: delegate to browse_url subprocess ---
    if stealth:
        # Stealth needs longer timeout for nodriver startup — use 60s minimum
        stealth_timeout = max(timeout, 60)
        result = _read_stealth(
            url, extract=extract, js_eval=js_eval, wait_seconds=kwargs.get("wait_seconds", 3),
            bypass_cache=bypass_cache, cache_ttl=cache_ttl, min_delay=min_delay,
            max_retries=max_retries, timeout=stealth_timeout
        )
        # SPN queueing for stealth path
        if result.get("success") and spn:
            try:
                canonical = canonicalize_url(url)
                spn_result = enqueue_spn(canonical)
                result["spn_queued"] = spn_result.get("queued", False)
            except Exception:
                result["spn_queued"] = False
        return result

    # --- Default path: four-tier capture pipeline ---
    if format not in ("text", "html", "full"):
        return {"error": f"Invalid format '{format}'. Must be: text, html, or full"}

    # Parse tags
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = tags or []

    # Canonicalize
    canonical_url = canonicalize_url(url)
    domain = extract_domain(canonical_url)

    # Rate limiting (shared state with other research tools)
    enforce_rate_limit(domain, BROWSE_RATE_FILE, default_delay=3, min_delay=2)

    # Four-tier capture
    capture = capture_page(canonical_url, timeout=timeout)

    article_text = capture.get("article_text", "")
    text_size = len(article_text)
    threshold = min_text_size if min_text_size is not None else MIN_TEXT_SIZE

    # Record domain request for rate-limit tracking
    was_blocked = not capture.get("success", False)
    record_request(domain, was_blocked=was_blocked)

    # Build response
    result = {
        "success": capture.get("success", False),
        "url": canonical_url,
        "domain": domain,
        "title": capture.get("title", ""),
        "metadata": capture.get("metadata", {}),
        "capture_method": capture.get("capture_method", "none"),
        "text_size": text_size,
        "word_count": len(article_text.split()) if article_text else 0,
        "tier_errors": capture.get("tier_errors", {}),
        "archive_status": "not_archived",
    }

    if canonical_url != url:
        result["original_url"] = url

    if not capture.get("success"):
        result["error"] = capture.get("error", "All capture tiers failed")
        if article_text:
            result["content"] = article_text
        return result

    # Check minimum text threshold
    if text_size < threshold:
        result["success"] = False
        result["error"] = (f"Insufficient content: {text_size} chars "
                          f"(minimum: {threshold}). Page may be paywalled or empty.")
        result["content"] = article_text
        return result

    # Junk content detection with SPA escalation
    is_junk, junk_reason = _detect_junk_content(
        result["title"], article_text, text_size, canonical_url
    )
    if is_junk:
        if "homepage-redirect" in (junk_reason or "") and capture.get("capture_method") == "http-readability":
            spa_capture = capture_page(canonical_url, timeout=timeout, start_tier=2)
            if spa_capture.get("success"):
                spa_text = spa_capture.get("article_text", "")
                spa_size = len(spa_text)
                spa_junk, spa_junk_reason = _detect_junk_content(
                    spa_capture.get("title", ""), spa_text, spa_size, canonical_url
                )
                if not spa_junk and spa_size >= threshold:
                    capture = spa_capture
                    article_text = spa_text
                    text_size = spa_size
                    result["title"] = spa_capture.get("title", "")
                    result["metadata"] = spa_capture.get("metadata", {})
                    result["capture_method"] = spa_capture.get("capture_method", "")
                    result["text_size"] = spa_size
                    result["word_count"] = len(spa_text.split())
                    result["success"] = True
                    result["spa_escalation"] = True
                    result["tier_errors"] = {
                        **capture.get("tier_errors", {}),
                        "tier1_junk": junk_reason,
                        **spa_capture.get("tier_errors", {}),
                    }
                else:
                    result["success"] = False
                    result["error"] = (f"Junk content detected: {junk_reason}. "
                                      f"SPA escalation also failed: {spa_junk_reason or 'insufficient content'}")
                    result["content"] = article_text
                    result["spa_escalation"] = "failed"
                    record_request(domain, was_blocked=True)
                    return result
            else:
                result["success"] = False
                result["error"] = (f"Junk content detected: {junk_reason}. "
                                  f"SPA escalation failed: {spa_capture.get('error', 'unknown')}")
                result["content"] = article_text
                result["spa_escalation"] = "failed"
                record_request(domain, was_blocked=True)
                return result
        else:
            result["success"] = False
            result["error"] = f"Junk content detected: {junk_reason}"
            result["content"] = article_text
            record_request(domain, was_blocked=True)
            return result

    # SPN queueing
    if spn:
        try:
            spn_result = enqueue_spn(canonical_url)
            result["spn_queued"] = spn_result.get("queued", False)
        except Exception:
            result["spn_queued"] = False

    # Format output
    if format == "text":
        result["content"] = article_text
    elif format == "html":
        result["content"] = capture.get("html", "")
    elif format == "full":
        result["content"] = article_text
        result["html"] = capture.get("html", "")
        result["html_size"] = capture.get("html_size", 0)

    # Optional archiving
    if archive and capture.get("success"):
        try:
            paths = save_capture(canonical_url, capture, archives_dir=ARCHIVES_DIR)
            result["archive_paths"] = paths

            if driver:
                _create_source_node(driver, canonical_url, domain, capture, paths, tags)
                result["archive_status"] = "created"
            else:
                result["archive_status"] = "files_saved_no_driver"
        except Exception as e:
            result["archive_status"] = f"archive_error: {e}"

    return result


# ============================================================
# Subprocess entry point (fallback)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = read_impl(**params)
    output(result)
