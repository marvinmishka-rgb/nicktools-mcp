"""Ingest manually saved web pages into the archive + knowledge graph.
---
description: Ingest user-saved HTML pages from uploads/websites/ into Source nodes
creates_nodes: [Source]
creates_edges: []
databases: [corcoran, lifestream]
---

When all automated capture tiers fail (Cloudflare CAPTCHA, aggressive bot
protection), the user can:
  1. Open the URL in a browser manually
  2. Solve the CAPTCHA / log in
  3. Save the page (Ctrl+S) to ClaudeFiles/uploads/websites/
  4. Call research("ingest_saved") to process the saved page

This tool reads saved HTML files, extracts article text via readability,
creates Source nodes in both databases, and moves the files to the
standard archive directory structure.

Supports:
  - Single file: ingest_saved(file="filename.html")
  - Scan inbox: ingest_saved() -- processes all .html/.htm files in uploads/websites/
  - With URL override: ingest_saved(file="page.html", url="https://original-url.com")
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))

import json
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from lib.io import setup_output, load_params, output
from lib.urls import canonicalize_url, extract_domain, SOURCE_TYPE_MAP
from lib.paths import SAVED_PAGES_DIR, ARCHIVES_DIR, ensure_dir
from lib.spn import enqueue_spn
from lib.db import GRAPH_DATABASE, ENTRY_DATABASE


def ingest_saved_impl(file=None, url=None, tags=None, entry_id=None,
                      spn=True, driver=None, **kwargs):
    """Ingest saved HTML page(s) into the archive system.

    Args:
        file: Specific filename in uploads/websites/ (optional -- omit to scan all)
        url: Original URL of the page (optional -- extracted from HTML if possible)
        tags: Tags for the Source node (list or comma-separated string)
        entry_id: Lifestream entry ID to wire CITES edge
        spn: Queue for Wayback SPN preservation (default True)
        driver: Neo4j driver (injected by dispatcher)

    Returns:
        dict with ingested files, Source nodes created, errors
    """
    # Parse tags
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags = tags or []
    tags.append("manual-capture")

    # Ensure inbox directory exists
    ensure_dir(SAVED_PAGES_DIR, "saved pages directory")

    # Find files to process
    if file:
        target = SAVED_PAGES_DIR / file
        if not target.exists():
            # Try without extension
            for ext in [".html", ".htm", ".mhtml"]:
                candidate = SAVED_PAGES_DIR / (file + ext)
                if candidate.exists():
                    target = candidate
                    break
            if not target.exists():
                return {
                    "error": f"File not found: {file}",
                    "inbox_path": str(SAVED_PAGES_DIR),
                    "hint": "Save the page to ClaudeFiles/uploads/websites/ first"
                }
        files = [target]
    else:
        # Scan inbox for all HTML files
        files = sorted(
            f for f in SAVED_PAGES_DIR.iterdir()
            if f.suffix.lower() in (".html", ".htm", ".mhtml")
        )
        if not files:
            return {
                "status": "empty",
                "inbox_path": str(SAVED_PAGES_DIR),
                "message": "No HTML files in uploads/websites/. Save a page there first.",
                "hint": "In Chrome: Ctrl+S -> 'Webpage, HTML Only' -> save to ClaudeFiles/uploads/websites/"
            }

    results = []
    for html_file in files:
        result = _ingest_one(html_file, url=url, tags=tags, entry_id=entry_id,
                             spn=spn, driver=driver)
        results.append(result)

    ingested = [r for r in results if r.get("status") == "ingested"]
    errors = [r for r in results if r.get("error")]

    return {
        "processed": len(results),
        "ingested": len(ingested),
        "errors": len(errors),
        "results": results,
        "inbox_path": str(SAVED_PAGES_DIR),
    }


def _ingest_one(html_file, url=None, tags=None, entry_id=None,
                spn=True, driver=None):
    """Process a single saved HTML file."""
    try:
        html = html_file.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"file": html_file.name, "error": f"Read failed: {e}"}

    if len(html) < 100:
        return {"file": html_file.name, "error": "File too small (<100 chars)"}

    # Try to extract the original URL from HTML
    detected_url = _detect_url(html)
    page_url = url or detected_url

    if not page_url:
        return {
            "file": html_file.name,
            "error": "Cannot determine original URL. Pass url= parameter.",
            "hint": "research('ingest_saved', {'file': '" + html_file.name + "', 'url': 'https://...'})"
        }

    canonical_url = canonicalize_url(page_url)
    domain = extract_domain(canonical_url)

    # Extract article text via readability
    article_text, title = _readability_extract(html)

    if not article_text or len(article_text) < 100:
        # Fallback: strip HTML tags
        article_text = _html_to_text(html)

    if not title:
        title = html_file.stem

    text_size = len(article_text)
    word_count = len(article_text.split()) if article_text else 0

    # Move file to standard archive structure
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    url_hash = hashlib.sha256(canonical_url.encode()).hexdigest()[:12]
    base_name = f"{date_str}_{url_hash}"

    safe_domain = domain.replace(":", "_").replace("/", "_")
    domain_dir = ARCHIVES_DIR / safe_domain
    ensure_dir(domain_dir, "archive domain directory")

    archive_html = domain_dir / f"{base_name}.html"
    archive_text = domain_dir / f"{base_name}_article.txt"
    archive_meta = domain_dir / f"{base_name}.meta.json"

    # Copy HTML to archive (don't move -- user might want to keep original)
    shutil.copy2(str(html_file), str(archive_html))

    # Save extracted text with header
    header = (
        f"{title}\n"
        f"URL: {canonical_url}\n"
        f"Captured: {date_str} via manual-save\n"
        f"{'=' * 80}\n\n"
    )
    archive_text.write_text(header + article_text, encoding="utf-8")

    # Save metadata
    meta = {
        "url": canonical_url,
        "domain": domain,
        "title": title,
        "captured_at": now.isoformat(),
        "capture_method": "manual-save",
        "html_size": len(html),
        "text_size": text_size,
        "word_count": word_count,
        "source_file": html_file.name,
        "tags": tags or [],
    }
    if url and detected_url and url != detected_url:
        meta["detected_url"] = detected_url
    archive_meta.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # Create Source nodes in both databases
    source_created = False
    if driver:
        try:
            _create_source_node(driver, canonical_url, domain, title, now,
                                text_size, article_text[:500],
                                str(archive_text), str(archive_html), tags)
            source_created = True
        except Exception as e:
            meta["source_node_error"] = str(e)[:200]

    # SPN queue
    spn_queued = False
    if spn:
        try:
            spn_result = enqueue_spn(canonical_url)
            spn_queued = spn_result.get("queued", False)
        except Exception:
            pass

    # Remove from inbox after successful processing
    try:
        html_file.unlink()
        # Also remove companion folder if Chrome saved "page_files/"
        companion_dir = html_file.parent / (html_file.stem + "_files")
        if companion_dir.exists() and companion_dir.is_dir():
            shutil.rmtree(str(companion_dir), ignore_errors=True)
    except Exception:
        pass  # Non-fatal

    return {
        "file": html_file.name,
        "status": "ingested",
        "url": canonical_url,
        "domain": domain,
        "title": title,
        "text_size": text_size,
        "word_count": word_count,
        "archive_path": str(archive_text),
        "source_node_created": source_created,
        "spn_queued": spn_queued,
    }


def _detect_url(html):
    """Try to detect the original URL from HTML content.

    Looks for canonical link, og:url meta tag, or base href.
    """
    import re

    # <link rel="canonical" href="...">
    m = re.search(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']', html, re.I)
    if m:
        return m.group(1)

    # <meta property="og:url" content="...">
    m = re.search(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']', html, re.I)
    if m:
        return m.group(1)

    # <base href="...">
    m = re.search(r'<base[^>]+href=["\']([^"\']+)["\']', html, re.I)
    if m:
        return m.group(1)

    return None


def _readability_extract(html):
    """Extract article text via readability-lxml."""
    try:
        from readability import Document
        from bs4 import BeautifulSoup

        doc = Document(html)
        article_html = doc.summary()
        title = doc.short_title() or ""

        soup = BeautifulSoup(article_html, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        lines = [line.strip() for line in text.splitlines()]
        article_text = "\n".join(line for line in lines if line)

        return article_text, title
    except Exception:
        return "", ""


def _html_to_text(html):
    """Fallback text extraction via regex."""
    import re
    import html as html_module

    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.I | re.DOTALL)
    cleaned = re.sub(r'<style[^>]*>.*?</style>', '', cleaned, flags=re.I | re.DOTALL)
    cleaned = re.sub(r'<[^>]+>', ' ', cleaned)
    text = html_module.unescape(cleaned)
    lines = [line.strip() for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def _create_source_node(driver, url, domain, title, now, text_size,
                        text_preview, text_path, html_path, tags):
    """Create Source node in both corcoran and lifestream."""
    source_type = SOURCE_TYPE_MAP.get(domain, SOURCE_TYPE_MAP.get("_default", "article"))

    params = {
        "url": url,
        "domain": domain,
        "title": title or "Untitled",
        "captured": now.isoformat(),
        "archive_status": "captured",
        "capture_method": "manual-save",
        "text_size": text_size,
        "text_preview": text_preview,
        "text_path": text_path,
        "html_path": html_path,
        "source_type": source_type,
        "tags": tags or [],
    }

    source_cypher = """
        MERGE (s:Source {url: $url})
        ON CREATE SET
            s.domain = $domain, s.title = $title,
            s.capturedAt = datetime($captured), s.lastCaptured = datetime($captured),
            s.captureCount = 1, s.archiveStatus = $archive_status,
            s.captureMethod = $capture_method, s.textSize = $text_size,
            s.textPreview = $text_preview, s.archivePath = $text_path,
            s.htmlPath = $html_path, s.sourceType = $source_type,
            s.tags = $tags
        ON MATCH SET
            s.lastCaptured = datetime($captured),
            s.captureCount = COALESCE(s.captureCount, 0) + 1,
            s.title = CASE WHEN $title <> 'Untitled' THEN $title ELSE s.title END,
            s.archiveStatus = $archive_status, s.captureMethod = $capture_method,
            s.textSize = $text_size, s.textPreview = $text_preview,
            s.archivePath = $text_path, s.htmlPath = $html_path,
            s.tags = CASE WHEN s.tags IS NULL THEN $tags
                     ELSE [x IN s.tags WHERE NOT x IN $tags] + $tags END
    """

    for db_name in [GRAPH_DATABASE, ENTRY_DATABASE]:
        with driver.session(database=db_name) as session:
            session.run(source_cypher, params)


# ============================================================
# Subprocess entry point
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = ingest_saved_impl(**params)
    output(result)
