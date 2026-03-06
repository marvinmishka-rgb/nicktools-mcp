"""Deprecated: use research("archive", {mode: "mhtml"}) instead.

save_page -- Full-page capture via nodriver CDP + article extraction.

Combines anti-detection browsing (nodriver) with CDP Page.captureSnapshot()
to get full MHTML, then runs the article extraction pipeline on the captured HTML.
If the direct capture is paywalled (server-side), automatically tries the
Wayback Machine snapshot URL.

This is the automated equivalent of "Brave Save Page Complete" + extract_saved_article.

Architecture: subprocess tool (uses nodriver, which requires its own event loop).

NOTE: This file is still used as a subprocess target by archive.py's mhtml mode.
Do not delete it while archive.py depends on it.
---
description: "[Deprecated] Full-page CDP capture — use archive(mode='mhtml') instead"
creates_nodes: [Source]
creates_edges: [CITES]
databases: [corcoran, lifestream]
---
"""
import json
import sys
import io
import os
import re
import hashlib
import asyncio
import warnings
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, quote

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.io import load_params, output
from lib.db import get_neo4j_driver, GRAPH_DATABASE, ENTRY_DATABASE
from lib.paths import ARCHIVES_DIR, ensure_dir
from lib.browsing import (BROWSE_RATE_FILE, BROWSE_DEFAULT_DELAY,
                           enforce_rate_limit, record_request)
from lib.archives import ARCHIVE_MIN_TEXT_SIZE
from lib.urls import extract_domain, canonicalize_url
from lib.entries import normalize_path

warnings.filterwarnings("ignore", category=ResourceWarning)

# Windows extended-length path prefix
WIN_LONG = "\\\\?\\"


def _long_path(p):
    s = str(p)
    if len(s) > 240 and not s.startswith(WIN_LONG):
        return WIN_LONG + s
    return s


async def _capture_page(url, wait_seconds=8):
    """Load URL with nodriver and capture full page content.

    Returns dict with html, mhtml (if available), title, final_url, metadata.
    """
    import nodriver as uc
    from nodriver.cdp import page as cdp_page

    browser = await uc.start(
        headless=True,
        browser_args=[
            '--no-first-run',
            '--no-default-browser-check',
            '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36'
        ]
    )
    try:
        tab = await browser.get(url)
        await tab.sleep(wait_seconds)

        # Get basic page info
        final_url = await tab.evaluate("window.location.href")
        if not isinstance(final_url, str):
            final_url = url
        title = await tab.evaluate("document.title")
        if not isinstance(title, str):
            title = ""

        # Get rendered DOM HTML
        html = await tab.get_content()
        if not isinstance(html, str):
            html = ""

        # Try CDP Page.captureSnapshot for full MHTML
        mhtml = None
        try:
            mhtml = await tab.send(cdp_page.capture_snapshot(format_="mhtml"))
            if not isinstance(mhtml, str):
                mhtml = None
        except Exception:
            pass  # MHTML capture is experimental, may fail

        # Extract metadata via JS
        article_meta = await tab.evaluate("""(() => {
            const get = (name) => {
                const el = document.querySelector(
                    `meta[property="${name}"], meta[name="${name}"]`
                );
                return el ? el.getAttribute('content') : null;
            };
            return {
                author: get('author') || get('article:author'),
                publishedDate: get('article:published_time') || get('datePublished') || get('date'),
                description: get('og:description') || get('description'),
                siteName: get('og:site_name'),
                ogUrl: get('og:url'),
                canonical: (() => {
                    const c = document.querySelector('link[rel="canonical"]');
                    return c ? c.getAttribute('href') : null;
                })()
            };
        })()""")
        if not isinstance(article_meta, dict):
            article_meta = {}

        return {
            "html": html,
            "mhtml": mhtml,
            "title": title,
            "final_url": final_url,
            "meta": article_meta,
        }
    finally:
        browser.stop()


def _extract_article_text(html):
    """Run three-tier article extraction on HTML string.

    Returns (text, method, para_count, paywall_patterns).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, 'lxml')

    # Detect paywall patterns before decomposing scripts
    paywall_patterns = []
    paywall_els = soup.find_all(class_=lambda c: c and 'paywall' in str(c).lower())
    if paywall_els:
        paywall_patterns.append(f"class-based paywall ({len(paywall_els)} elements)")
    sub_els = soup.find_all(class_=lambda c: c and any(
        kw in str(c).lower() for kw in ['subscriber', 'premium', 'locked', 'gated']
    ))
    if sub_els:
        paywall_patterns.append(f"subscriber/premium markers ({len(sub_els)} elements)")
    signin_els = soup.find_all(class_=lambda c: c and any(
        kw in str(c).lower() for kw in ['signin-wall', 'login-wall', 'barrier']
    ))
    if signin_els:
        paywall_patterns.append(f"sign-in barrier ({len(signin_els)} elements)")

    # Check for paywall text signals
    body_text = soup.get_text().lower()
    paywall_signals = ['subscribe now', 'already a subscriber', 'subscription required',
                       'sign in to read', 'premium content', 'this content is only available']
    signal_hits = [s for s in paywall_signals if s in body_text]
    if signal_hits:
        paywall_patterns.append(f"text signals: {', '.join(signal_hits)}")

    # Remove scripts/styles for extraction
    for tag in soup(['script', 'style', 'noscript', 'iframe']):
        tag.decompose()

    # Tier 1: JSON-LD articleBody
    schema_match = re.search(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if schema_match:
        body = schema_match.group(1)
        body = body.encode().decode('unicode_escape', errors='replace')
        if len(body) > 500:
            paragraphs = [p.strip() for p in body.split('\n') if p.strip()]
            return '\n\n'.join(paragraphs), 'json-ld-articleBody', len(paragraphs), paywall_patterns

    # Tier 2: Semantic container
    container = soup.find('article') or soup.find('main')
    if container:
        paragraphs = []
        for p in container.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) > 15:
                paragraphs.append(text)
        if len(paragraphs) >= 3:
            return '\n\n'.join(paragraphs), 'semantic-container', len(paragraphs), paywall_patterns

    # Tier 3: All <p> tags
    paragraphs = []
    for p in soup.find_all('p'):
        text = p.get_text(strip=True)
        if len(text) > 20:
            paragraphs.append(text)
    if paragraphs:
        return '\n\n'.join(paragraphs), 'all-paragraphs', len(paragraphs), paywall_patterns

    return '', 'none', 0, paywall_patterns


def _check_wayback(url):
    """Check Wayback Machine for available snapshot. Returns snapshot URL or None."""
    import urllib.request
    import urllib.error
    import socket

    try:
        encoded = quote(url, safe=':/')
        cdx_url = f"http://archive.org/wayback/available?url={encoded}"
        req = urllib.request.Request(cdx_url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; research-tool/1.0)"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        snapshot = data.get("archived_snapshots", {}).get("closest", {})
        snap_url = snapshot.get("url", "")

        if snap_url and snapshot.get("available"):
            return snap_url
    except Exception:
        pass
    return None


def _save_files(domain, url_hash, html, mhtml, extracted_text, meta):
    """Save all captured files to archives directory."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    domain_dir = ARCHIVES_DIR / domain
    ensure_dir(domain_dir, "archive domain directory")

    base_name = f"{date_str}_{url_hash}"
    paths = {}

    # Save HTML
    html_path = domain_dir / f"{base_name}.html"
    html_path.write_text(html, encoding='utf-8')
    paths["html"] = str(html_path)

    # Save MHTML if available
    if mhtml:
        mhtml_path = domain_dir / f"{base_name}.mhtml"
        mhtml_path.write_text(mhtml, encoding='utf-8')
        paths["mhtml"] = str(mhtml_path)

    # Save extracted text with header
    if extracted_text:
        text_path = domain_dir / f"{base_name}_extracted.txt"
        header_lines = [
            meta.get("title", "Untitled"),
        ]
        if meta.get("author"):
            header_lines.append(f"By {meta['author']}")
        if meta.get("siteName"):
            header_lines.append(f"Source: {meta['siteName']}")
        if meta.get("publishedDate"):
            header_lines.append(f"Published: {meta['publishedDate']}")
        if meta.get("url"):
            header_lines.append(f"URL: {meta['url']}")
        header_lines.append(f"Captured: {date_str} via save_page (nodriver CDP)")
        header_lines.append("=" * 80)

        full_text = '\n'.join(header_lines) + '\n\n' + extracted_text
        text_path.write_text(full_text, encoding='utf-8')
        paths["text"] = str(text_path)

    # Save metadata
    meta_path = domain_dir / f"{base_name}.meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
    paths["meta"] = str(meta_path)

    return paths


def _wire_neo4j(url, domain, meta, paths, extracted_chars, method,
                paywall_patterns, entry_id=None, context=None, tags=None,
                via_wayback=False, wayback_url=None):
    """Create/update Source node in both databases."""
    # Canonicalize URL to prevent www./trailing-slash duplicates
    url = canonicalize_url(url)
    driver = get_neo4j_driver()
    now = datetime.now(timezone.utc)

    try:
        # Corcoran database (primary for SUPPORTED_BY edges)
        with driver.session(database=GRAPH_DATABASE) as session:
            session.run(
                "MERGE (s:Source {url: $url}) "
                "ON CREATE SET s.domain = $domain, s.createdAt = datetime() "
                "SET s.archiveStatus = 'captured', "
                "    s.captureMethod = $method, "
                "    s.title = $title, "
                "    s.author = $author, "
                "    s.publishedDate = $pubdate, "
                "    s.textFile = $textFile, "
                "    s.htmlFile = $htmlFile, "
                "    s.capturedAt = datetime(), "
                "    s.textSize = $textSize, "
                "    s.context = $context, "
                "    s.tags = $tags, "
                "    s.paywallPatterns = $patterns, "
                "    s.extractionMethod = $extraction, "
                "    s.viaWayback = $viaWayback, "
                "    s.waybackUrl = COALESCE($waybackUrl, s.waybackUrl), "
                "    s.failureReason = null ",
                {
                    "url": url,
                    "domain": domain,
                    "title": meta.get("title", ""),
                    "author": meta.get("author", ""),
                    "pubdate": meta.get("publishedDate", ""),
                    "textFile": normalize_path(paths.get("text", "")),
                    "htmlFile": normalize_path(paths.get("html", "")),
                    "textSize": extracted_chars,
                    "context": context or "",
                    "tags": tags or [],
                    "patterns": paywall_patterns,
                    "extraction": method,
                    "viaWayback": via_wayback,
                    "waybackUrl": wayback_url,
                    "method": "save-page-wayback" if via_wayback else "save-page-cdp",
                }
            )

            # Wire CITES edge if entry_id provided
            if entry_id:
                session.run(
                    "MATCH (s:Source {url: $url}) "
                    "WITH s "
                    "MATCH (e:StreamEntry {id: $eid}) "
                    "MERGE (e)-[r:CITES]->(s) "
                    "SET r.context = $context, r.createdAt = datetime()",
                    {"url": url, "eid": entry_id, "context": context or ""}
                )

        # Lifestream database
        with driver.session(database=ENTRY_DATABASE) as session:
            session.run(
                "MERGE (s:Source {url: $url}) "
                "ON CREATE SET s.domain = $domain "
                "SET s.title = $title, "
                "    s.archiveStatus = 'captured', "
                "    s.captureMethod = $method, "
                "    s.textSize = $textSize, "
                "    s.capturedAt = datetime()",
                {
                    "url": url,
                    "domain": domain,
                    "title": meta.get("title", ""),
                    "textSize": extracted_chars,
                    "method": "save-page-wayback" if via_wayback else "save-page-cdp",
                }
            )

            if entry_id:
                session.run(
                    "MATCH (e:StreamEntry {id: $eid}) "
                    "MERGE (s:Source {url: $url}) "
                    "ON CREATE SET s.domain = $domain "
                    "MERGE (e)-[r:CITES]->(s) "
                    "SET r.context = $context",
                    {"url": url, "eid": entry_id, "domain": domain, "context": context or ""}
                )

        return "Source node created/updated"
    except Exception as e:
        return f"Neo4j error: {e}"
    finally:
        driver.close()


def main():
    params = load_params()
    url = params["url"]
    entry_id = params.get("entry_id")
    context = params.get("context")
    tags = params.get("tags", [])
    wait_seconds = params.get("wait_seconds", 8)
    try_wayback = params.get("try_wayback", True)

    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]

    # Rate limit
    rate_file = Path(BROWSE_RATE_FILE)
    rate_info = enforce_rate_limit(domain, rate_file, default_delay=BROWSE_DEFAULT_DELAY)

    result = {
        "url": url,
        "domain": domain,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }

    # -- Step 1: Capture page with nodriver --
    old_stderr = sys.stderr
    sys.stderr = open(os.devnull, 'w')
    try:
        capture = asyncio.run(_capture_page(url, wait_seconds))
    finally:
        sys.stderr.close()
        sys.stderr = old_stderr

    html = capture["html"]
    mhtml = capture.get("mhtml")
    result["title"] = capture["title"]
    result["html_size"] = len(html)
    result["mhtml_captured"] = mhtml is not None
    if mhtml:
        result["mhtml_size"] = len(mhtml)

    # Record request
    record_request(domain, rate_file, was_blocked=len(html) < 500)

    # -- Step 2: Extract article text --
    text, method, para_count, paywall_patterns = _extract_article_text(html)
    result["extraction_method"] = method
    result["paragraph_count"] = para_count
    result["extracted_chars"] = len(text)
    result["paywall_patterns"] = paywall_patterns

    # Build metadata for file headers
    meta = {
        "title": capture["title"],
        "url": url,
        "author": capture["meta"].get("author"),
        "publishedDate": capture["meta"].get("publishedDate"),
        "siteName": capture["meta"].get("siteName"),
    }

    # -- Step 3: Check if extraction is sufficient --
    is_paywalled = len(paywall_patterns) > 0 or len(text) < ARCHIVE_MIN_TEXT_SIZE
    # Trigger Wayback if: very short text OR paywalled with modest text
    # (server-side paywalls deliver ~1-2 paragraphs before cutting off)
    text_too_short = len(text) < 500 or (is_paywalled and len(text) < 3000)
    via_wayback = False
    wayback_url = None

    if text_too_short and try_wayback:
        # Server-side paywall or bot block -- try Wayback Machine
        result["direct_capture_insufficient"] = True
        result["trying_wayback"] = True

        snap_url = _check_wayback(url)
        if snap_url:
            wayback_url = snap_url
            result["wayback_snapshot"] = snap_url

            # Check if web.archive.org is reachable
            import socket
            can_reach = False
            try:
                sock = socket.create_connection(("web.archive.org", 443), timeout=5)
                sock.close()
                can_reach = True
            except (socket.timeout, OSError):
                pass

            if can_reach:
                # Re-capture from Wayback
                old_stderr2 = sys.stderr
                sys.stderr = open(os.devnull, 'w')
                try:
                    wb_capture = asyncio.run(_capture_page(snap_url, wait_seconds + 3))
                finally:
                    sys.stderr.close()
                    sys.stderr = old_stderr2

                wb_text, wb_method, wb_paras, wb_paywall = _extract_article_text(wb_capture["html"])
                if len(wb_text) > len(text):
                    # Wayback version is better -- use it
                    html = wb_capture["html"]
                    mhtml = wb_capture.get("mhtml")
                    text = wb_text
                    method = wb_method
                    para_count = wb_paras
                    paywall_patterns = wb_paywall
                    via_wayback = True

                    # Update metadata -- unwrap Wayback URL from title/meta
                    if wb_capture["meta"].get("canonical"):
                        meta["url"] = url  # keep original URL
                    meta["title"] = wb_capture["title"] or capture["title"]
                    if wb_capture["meta"].get("author"):
                        meta["author"] = wb_capture["meta"]["author"]
                    if wb_capture["meta"].get("publishedDate"):
                        meta["publishedDate"] = wb_capture["meta"]["publishedDate"]

                    result["wayback_used"] = True
                    result["extraction_method"] = method
                    result["paragraph_count"] = para_count
                    result["extracted_chars"] = len(text)
                    result["paywall_patterns"] = paywall_patterns
                else:
                    result["wayback_no_improvement"] = True
            else:
                result["wayback_unreachable"] = True
                result["hint"] = (
                    f"Wayback snapshot exists but web.archive.org is blocked. "
                    f"Open in Brave and Save Page Complete, then use extract_saved_article: {snap_url}"
                )

    # -- Step 4: Save files --
    if text and len(text) >= 100:
        paths = _save_files(domain, url_hash, html, mhtml, text, meta)
        result["archived_files"] = paths
        result["status"] = "captured"

        # -- Step 5: Wire Neo4j --
        neo4j_result = _wire_neo4j(
            url=url, domain=domain, meta=meta, paths=paths,
            extracted_chars=len(text), method=method,
            paywall_patterns=paywall_patterns,
            entry_id=entry_id, context=context, tags=tags,
            via_wayback=via_wayback, wayback_url=wayback_url
        )
        result["neo4j"] = neo4j_result
        if entry_id:
            result["cites"] = entry_id

        # Text preview
        result["text_preview"] = text[:500]
    else:
        # Save whatever we got (even if insufficient) for debugging
        paths = _save_files(domain, url_hash, html, mhtml, text if text else "", meta)
        result["archived_files"] = paths
        result["status"] = "failed"
        result["failure_reason"] = "server-side-paywall" if is_paywalled else "insufficient-content"
        result["WARNING"] = (
            f"Only {len(text)} chars extracted. Site likely uses server-side paywall. "
            f"Try: (1) Wayback Machine in Brave, (2) WebFetch, (3) manual copy."
        )
        if wayback_url:
            result["wayback_fallback_url"] = wayback_url

    output(result)


if __name__ == "__main__":
    main()
