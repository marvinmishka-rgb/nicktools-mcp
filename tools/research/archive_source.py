#!/usr/bin/env python3
"""Deprecated: use research("archive", {mode: "full"}) instead.

Standalone archive_source tool.
Archives a web page locally with nodriver (anti-detection).
Creates Source node in Neo4j lifestream + CITES edge if entry_id provided.

NOTE: This file is still used as a subprocess target by archive.py's full mode.
Do not delete it while archive.py depends on it.
---
description: "[Deprecated] Archive web page — use archive(mode='full') instead"
creates_nodes: [Source, File]
creates_edges: [ARCHIVED_AS, CITES]
databases: [corcoran, lifestream]
---
"""

import json, sys, io, os, time, hashlib, re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.db import get_neo4j_driver, GRAPH_DATABASE, ENTRY_DATABASE
from lib.io import load_params, output
from lib.paths import ARCHIVES_DIR, ensure_dir
from lib.browsing import (BROWSE_RATE_FILE, BROWSE_DEFAULT_DELAY,
                           enforce_rate_limit, record_request)
from lib.archives import ARCHIVE_MIN_TEXT_SIZE
from lib.spn import enqueue_spn
from lib.urls import canonicalize_url, extract_domain as config_extract_domain, SOURCE_TYPE_MAP


def _is_pdf_url(url, timeout=10):
    """Check if URL points to a PDF via extension and/or HEAD request.

    Returns True if URL likely points to a PDF document.
    """
    parsed = urlparse(url)
    if parsed.path.lower().endswith('.pdf'):
        return True
    try:
        import requests
        resp = requests.head(url, timeout=timeout, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/145.0.0.0"})
        content_type = resp.headers.get('Content-Type', '')
        return 'application/pdf' in content_type
    except Exception:
        return False


def _archive_pdf(url, entry_id=None, context=None, tags=None, archives_dir=None, min_text_size=200, timeout_seconds=60):
    """Download a PDF, extract text, create Source node.

    This branch runs BEFORE nodriver since nodriver can't render PDFs.

    Returns:
        result dict with status, paths, text_size, etc.
        Returns None if download or extraction fails catastrophically.
    """
    import requests

    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]

    if archives_dir is None:
        archives_dir = ARCHIVES_DIR
    archives_dir = Path(archives_dir)
    domain_dir = archives_dir / domain.replace(":", "_").replace("/", "_")
    ensure_dir(domain_dir, "archive domain directory")

    base_name = f"{date_str}_{url_hash}"
    pdf_path = domain_dir / f"{base_name}.pdf"
    text_path = domain_dir / f"{base_name}_extracted.txt"
    meta_path = domain_dir / f"{base_name}.meta.json"

    # Step 1: Download the PDF binary
    try:
        resp = requests.get(url, timeout=timeout_seconds, allow_redirects=True,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/145.0.0.0"},
                            stream=True)
        resp.raise_for_status()

        # Verify it's actually a PDF (content-type or magic bytes)
        content_type = resp.headers.get('Content-Type', '')
        pdf_data = resp.content

        if not pdf_data or len(pdf_data) < 100:
            return {"error": "PDF download returned empty or tiny response",
                    "failure_type": "download-failed", "url": url}

        pdf_path.write_bytes(pdf_data)
    except Exception as e:
        return {"error": f"PDF download failed: {e}",
                "failure_type": "download-failed", "url": url}

    # Step 2: Extract text with pdfplumber (fallback to PyMuPDF)
    extracted_text = ""
    extraction_method = "none"
    page_count = 0

    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            pages_text = []
            for i, page in enumerate(pdf.pages):
                page_text = page.extract_text()
                if page_text:
                    pages_text.append(f"--- Page {i+1} ---\n{page_text}")
            extracted_text = "\n\n".join(pages_text)
            extraction_method = "pdfplumber"
    except Exception as e_plumber:
        # Fallback to PyMuPDF
        try:
            import fitz
            doc = fitz.open(str(pdf_path))
            page_count = len(doc)
            pages_text = []
            for i, page in enumerate(doc):
                page_text = page.get_text()
                if page_text and page_text.strip():
                    pages_text.append(f"--- Page {i+1} ---\n{page_text}")
            extracted_text = "\n\n".join(pages_text)
            extraction_method = "pymupdf"
            doc.close()
        except Exception as e_fitz:
            return {"error": f"PDF text extraction failed. pdfplumber: {e_plumber}, PyMuPDF: {e_fitz}",
                    "failure_type": "extraction-failed", "url": url,
                    "pdf_path": str(pdf_path), "pdf_size": len(pdf_data)}

    text_size = len(extracted_text)

    # Step 3: Save extracted text with header
    title = Path(urlparse(url).path).stem or "Untitled PDF"
    header_lines = [
        title,
        f"URL: {url}",
        f"Pages: {page_count}",
        f"Captured: {date_str} via archive_source (PDF branch)",
        f"Extraction: {extraction_method}",
        "=" * 80,
    ]
    full_text = '\n'.join(header_lines) + '\n\n' + extracted_text
    text_path.write_text(full_text, encoding='utf-8')

    # Save metadata
    meta = {
        "url": url,
        "domain": domain,
        "title": title,
        "captured_at": now.isoformat(),
        "url_hash": url_hash,
        "pdf_file": str(pdf_path),
        "text_file": str(text_path),
        "pdf_size": len(pdf_data),
        "text_size": text_size,
        "page_count": page_count,
        "extraction_method": extraction_method,
        "text_preview": extracted_text[:500],
        "entry_id": entry_id,
        "context": context,
        "tags": tags or [],
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')

    # Step 4: Determine capture status
    capture_failed = text_size < min_text_size
    archive_status = 'failed' if capture_failed else 'captured'
    failure_reason = 'insufficient-content' if capture_failed else None

    # Step 5: Wire into Neo4j
    canonical_url = canonicalize_url(url)
    original_url = url if canonical_url != url else None
    driver = get_neo4j_driver()

    try:
        for db_name in [GRAPH_DATABASE, ENTRY_DATABASE]:
            with driver.session(database=db_name) as session:
                session.run(
                    """MERGE (s:Source {url: $url})
                    ON CREATE SET
                        s.domain = $domain,
                        s.title = $title,
                        s.capturedAt = datetime($captured),
                        s.lastCaptured = datetime($captured),
                        s.captureCount = 1,
                        s.sourceType = 'pdf-document',
                        s.archiveStatus = $archive_status,
                        s.archivePath = $text_path,
                        s.pdfPath = $pdf_path,
                        s.textSize = $text_size,
                        s.textPreview = $text_preview,
                        s.pageCount = $page_count,
                        s.extractionMethod = $extraction_method,
                        s.failureReason = $failure_reason,
                        s.originalUrl = $original_url,
                        s.tags = $tags
                    ON MATCH SET
                        s.lastCaptured = datetime($captured),
                        s.captureCount = COALESCE(s.captureCount, 0) + 1,
                        s.title = $title,
                        s.sourceType = 'pdf-document',
                        s.archiveStatus = $archive_status,
                        s.archivePath = $text_path,
                        s.pdfPath = $pdf_path,
                        s.textSize = $text_size,
                        s.textPreview = $text_preview,
                        s.pageCount = $page_count,
                        s.extractionMethod = $extraction_method,
                        s.failureReason = $failure_reason,
                        s.originalUrl = COALESCE($original_url, s.originalUrl),
                        s.tags = CASE WHEN s.tags IS NULL THEN $tags
                                 ELSE [x IN s.tags WHERE NOT x IN $tags] + $tags END
                    """,
                    {
                        "url": canonical_url,
                        "domain": domain,
                        "title": title,
                        "captured": now.isoformat(),
                        "archive_status": archive_status,
                        "text_path": str(text_path),
                        "pdf_path": str(pdf_path),
                        "text_size": text_size,
                        "text_preview": extracted_text[:500],
                        "page_count": page_count,
                        "extraction_method": extraction_method,
                        "failure_reason": failure_reason,
                        "original_url": original_url,
                        "tags": tags or [],
                    }
                )

                # Wire CITES edge if entry_id provided (only on lifestream)
                if entry_id and db_name == ENTRY_DATABASE:
                    session.run(
                        """MATCH (e:StreamEntry {id: $entry_id}), (s:Source {url: $url})
                        MERGE (e)-[r:CITES]->(s)
                        SET r.context = $context, r.capturedAt = datetime($captured)""",
                        {"entry_id": entry_id, "url": canonical_url,
                         "context": context or "", "captured": now.isoformat()}
                    )
    finally:
        driver.close()

    # Queue for SPN preservation (fire-and-forget, async worker handles submission)
    spn_result = enqueue_spn(url)

    result = {
        "status": archive_status,
        "source_type": "pdf-document",
        "url": canonical_url,
        "original_url": original_url,
        "title": title,
        "domain": domain,
        "captured_at": now.isoformat(),
        "pdf_size": len(pdf_data),
        "text_size": text_size,
        "page_count": page_count,
        "extraction_method": extraction_method,
        "pdf_path": str(pdf_path),
        "text_path": str(text_path),
        "neo4j": "Source node created in corcoran + lifestream",
        "cites": entry_id or "none",
    }
    if spn_result:
        result["spn"] = {
            "status": spn_result.get("status", "unknown"),
            "wayback_url": spn_result.get("wayback_url"),
            "detail": spn_result.get("detail", ""),
        }
    if capture_failed:
        result["WARNING"] = f"Only {text_size} chars extracted from {page_count} pages. PDF may be scanned/image-based."
        result["failure_reason"] = failure_reason

    return result


CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def _html_to_text(html):
    """Extract visible text from HTML, stripping tags, scripts, styles."""
    import html as html_module
    # Remove script, style, noscript blocks
    cleaned = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'<style[^>]*>.*?</style>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'<noscript[^>]*>.*?</noscript>', '', cleaned, flags=re.IGNORECASE | re.DOTALL)
    # Replace block elements with newlines
    cleaned = re.sub(r'<(?:br|p|div|h[1-6]|li|tr|blockquote|hr)[^>]*/?>', '\n', cleaned, flags=re.IGNORECASE)
    # Strip remaining tags
    cleaned = re.sub(r'<[^>]+>', '', cleaned)
    # Decode HTML entities
    text = html_module.unescape(cleaned)
    # Clean up whitespace: collapse runs of blank lines
    lines = [line.strip() for line in text.splitlines()]
    text = '\n'.join(line for line in lines if line)
    return text


def _extract_meta_from_html(html):
    """Extract article metadata from HTML meta tags via regex."""
    def get_meta(names):
        for name in names:
            # Try property="name" content="value" order
            match = re.search(
                rf'<meta\s+(?:property|name)=["\'](?:{re.escape(name)})["\']\s+content=["\']([^"\']*)["\']',
                html, re.IGNORECASE
            )
            if not match:
                # Try content="value" property="name" order
                match = re.search(
                    rf'<meta\s+content=["\']([^"\']*)["\']\s+(?:property|name)=["\'](?:{re.escape(name)})["\']',
                    html, re.IGNORECASE
                )
            if match:
                return match.group(1)
        return None

    return {
        "author": get_meta(["author", "article:author", "og:article:author"]),
        "publishedDate": get_meta(["article:published_time", "datePublished",
                                   "og:article:published_time", "date"]),
        "description": get_meta(["og:description", "description"]),
        "siteName": get_meta(["og:site_name"]),
    }


def _capture_via_chrome_cli(url, entry_id=None, context=None, tags=None,
                             wait_seconds=5, archives_dir=None, min_text_size=200, timeout_seconds=60):
    """Fallback capture using Chrome's built-in --headless --dump-dom mode.

    Bypasses nodriver entirely. Chrome renders the page and outputs the DOM
    to stdout. We parse HTML for text and metadata. Used when nodriver fails
    to connect to its spawned browser instance.

    Returns:
        Same (meta, title, final_url, html_size, text_size) tuple as archive_url.
    """
    import subprocess
    import tempfile
    import shutil

    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    date_str = now.strftime("%Y-%m-%d")

    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]

    if archives_dir is None:
        archives_dir = ARCHIVES_DIR
    archives_dir = Path(archives_dir)
    domain_dir = archives_dir / domain.replace(":", "_").replace("/", "_")
    ensure_dir(domain_dir, "archive domain directory")

    base_name = f"{date_str}_{url_hash}"
    html_path = domain_dir / f"{base_name}.html"
    text_path = domain_dir / f"{base_name}.txt"
    meta_path = domain_dir / f"{base_name}.meta.json"

    # Use a unique temp profile to avoid conflicts with other Chrome instances
    temp_profile = tempfile.mkdtemp(prefix="chrome_fallback_")
    wait_ms = max(wait_seconds * 1000, 3000)

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
            capture_output=True, timeout=timeout_seconds + wait_seconds + 5,
            encoding='utf-8', errors='replace'
        )

        html_content = result.stdout
        if not html_content or len(html_content) < 50:
            raise RuntimeError(
                f"Chrome --dump-dom returned {len(html_content) if html_content else 0} chars. "
                f"Exit code: {result.returncode}. Stderr: {result.stderr[:200]}"
            )
    finally:
        try:
            shutil.rmtree(temp_profile, ignore_errors=True)
        except Exception:
            pass

    # Extract text from rendered HTML
    text_content = _html_to_text(html_content)

    # Extract title
    title_match = re.search(r'<title[^>]*>(.*?)</title>', html_content, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else ""

    # Extract meta tags
    article_meta = _extract_meta_from_html(html_content)

    # Save files
    html_path.write_text(html_content, encoding='utf-8')
    text_path.write_text(text_content, encoding='utf-8')

    meta = {
        "url": url,
        "final_url": url,  # --dump-dom doesn't report redirects
        "domain": domain,
        "title": title,
        "captured_at": now.isoformat(),
        "timestamp": timestamp,
        "url_hash": url_hash,
        "html_file": str(html_path),
        "text_file": str(text_path),
        "meta_file": str(meta_path),
        "html_size": len(html_content),
        "text_size": len(text_content),
        "text_preview": text_content[:500],
        "entry_id": entry_id,
        "context": context,
        "tags": tags or [],
        "author": article_meta.get("author"),
        "publishedDate": article_meta.get("publishedDate"),
        "siteName": article_meta.get("siteName"),
        "capture_method": "chrome-cli-fallback",
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')

    return meta, title, url, len(html_content), len(text_content)


async def archive_url(url, entry_id=None, context=None, tags=None, wait_seconds=5, archives_dir=None, min_text_size=200, timeout_seconds=60):
    """
    Archives a URL using nodriver (anti-detection).
    Returns (meta, title, final_url, html_size, text_size) on success.
    """
    import nodriver as uc
    import warnings

    warnings.filterwarnings("ignore", category=ResourceWarning)

    parsed = urlparse(url)
    domain = parsed.netloc.replace("www.", "")
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    date_str = now.strftime("%Y-%m-%d")

    rate_file = Path(BROWSE_RATE_FILE)
    rate_info = enforce_rate_limit(domain, rate_file, default_delay=BROWSE_DEFAULT_DELAY)
    if rate_info["waited"]:
        print(json.dumps({
            "rate_limited": True,
            "domain": domain,
            "waiting_seconds": rate_info["wait_seconds"],
            "tool": "archive_source",
        }, indent=2))

    # Create deterministic hash for filename (URL + timestamp for uniqueness)
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]

    # Build archive directory
    if archives_dir is None:
        archives_dir = ARCHIVES_DIR
    archives_dir = Path(archives_dir)
    domain_dir = archives_dir / domain.replace(":", "_").replace("/", "_")
    ensure_dir(domain_dir, "archive domain directory")

    # File paths
    base_name = f"{date_str}_{url_hash}"
    html_path = domain_dir / f"{base_name}.html"
    text_path = domain_dir / f"{base_name}.txt"
    meta_path = domain_dir / f"{base_name}.meta.json"

    browser = await uc.start(
        headless=True,
        browser_args=[
            '--no-first-run',
            '--no-default-browser-check',
            '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36'
        ]
    )
    try:
        page = await browser.get(url)
        await page.sleep(wait_seconds)

        # Capture everything -- nodriver evaluate() can return ExceptionDetails on failure
        final_url = await page.evaluate("window.location.href")
        if not isinstance(final_url, str):
            final_url = url  # fallback to input URL

        title = await page.evaluate("document.title")
        if not isinstance(title, str):
            title = ""

        html_content = await page.get_content()
        if not isinstance(html_content, str):
            html_content = ""

        text_content = await page.evaluate("document.body.innerText")
        if not isinstance(text_content, str):
            text_content = ""

        # Save HTML
        html_path.write_text(html_content, encoding='utf-8')

        # Save extracted text
        text_path.write_text(text_content, encoding='utf-8')

        # Extract structured metadata from HTML meta tags
        article_meta = await page.evaluate("""(() => {
            const get = (name) => {
                const el = document.querySelector(
                    `meta[property="${name}"], meta[name="${name}"]`
                );
                return el ? el.getAttribute('content') : null;
            };
            return {
                author: get('author') || get('article:author') || get('og:article:author'),
                publishedDate: get('article:published_time') || get('datePublished')
                    || get('og:article:published_time') || get('date'),
                description: get('og:description') || get('description'),
                siteName: get('og:site_name'),
            };
        })()""")

        # Normalize article_meta -- nodriver sometimes returns unexpected types
        if not isinstance(article_meta, dict):
            article_meta = None

        # Build metadata
        meta = {
            "url": url,
            "final_url": final_url,
            "domain": domain,
            "title": title,
            "captured_at": now.isoformat(),
            "timestamp": timestamp,
            "url_hash": url_hash,
            "html_file": str(html_path),
            "text_file": str(text_path),
            "meta_file": str(meta_path),
            "html_size": len(html_content),
            "text_size": len(text_content),
            "text_preview": text_content[:500],
            "entry_id": entry_id,
            "context": context,
            "tags": tags or [],
            "author": article_meta.get("author") if article_meta else None,
            "publishedDate": article_meta.get("publishedDate") if article_meta else None,
            "siteName": article_meta.get("siteName") if article_meta else None,
        }
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')

        return meta, title, final_url, len(html_content), len(text_content)
    finally:
        browser.stop()


def main():
    """
    CLI entry point. Reads params from stdin, archives URL, writes result to stdout.
    """
    import asyncio
    import warnings

    warnings.filterwarnings("ignore", category=ResourceWarning)

    try:
        params = load_params()
        url = params.get("url")
        entry_id = params.get("entry_id")
        context = params.get("context")
        tags = params.get("tags", [])
        wait_seconds = params.get("wait_seconds", 5)
        archives_dir = params.get("archives_dir")
        min_text_size = params.get("min_text_size", ARCHIVE_MIN_TEXT_SIZE)
        timeout_seconds = params.get("timeout_seconds", 60)

        # -- PDF detection: run BEFORE nodriver (nodriver can't render PDFs) --
        if _is_pdf_url(url):
            pdf_result = _archive_pdf(
                url, entry_id=entry_id, context=context, tags=tags,
                archives_dir=archives_dir, min_text_size=min_text_size, timeout_seconds=timeout_seconds
            )
            output(pdf_result)
            return

        # Try nodriver first; fall back to Chrome CLI if nodriver fails to connect
        capture_method = "nodriver"
        old_stderr = sys.stderr
        sys.stderr = open(os.devnull, 'w')
        try:
            meta, title, final_url, html_size, text_size = asyncio.run(
                archive_url(url, entry_id, context, tags, wait_seconds, archives_dir, min_text_size, timeout_seconds)
            )
        except Exception as nodriver_err:
            # Restore stderr before fallback so we can log
            sys.stderr.close()
            sys.stderr = old_stderr
            old_stderr = None  # prevent double-close in finally

            nodriver_msg = str(nodriver_err)
            print(json.dumps({
                "nodriver_failed": True,
                "error": nodriver_msg,
                "attempting_fallback": "chrome-cli",
            }))

            try:
                meta, title, final_url, html_size, text_size = _capture_via_chrome_cli(
                    url, entry_id=entry_id, context=context, tags=tags,
                    wait_seconds=wait_seconds, archives_dir=archives_dir,
                    min_text_size=min_text_size, timeout_seconds=timeout_seconds
                )
                capture_method = "chrome-cli-fallback"
            except Exception as fallback_err:
                output({
                    "error": "Both capture methods failed",
                    "nodriver_error": nodriver_msg,
                    "chrome_cli_error": str(fallback_err),
                    "url": url,
                    "hint": "Try WebFetch or Chrome DevTools MCP manually",
                })
                return
        finally:
            if old_stderr is not None:
                sys.stderr.close()
                sys.stderr = old_stderr

        # Capture validation gate -- three checks:
        # 1. Raw text size below minimum (empty page, bot block)
        # 2. Paywall detection: text exceeds minimum but contains paywall signals
        #    and has high HTML-to-text ratio (nav chrome inflates text_size)
        # 3. Browser error page detection: DNS failures, connection errors, HTTP errors
        text_content_raw = Path(meta["text_file"]).read_text(encoding='utf-8')
        text_lower = text_content_raw.lower()

        # Browser error page detection (Chrome error messages)
        browser_error_signals = [
            'err_name_not_resolved', 'err_connection_refused', 'err_connection_timed_out',
            'err_connection_reset', 'err_ssl_protocol_error', 'err_cert_authority_invalid',
            'err_address_unreachable', 'err_network_changed', 'err_internet_disconnected',
            "this site can't be reached", "this site can\u2019t be reached",
            'dns_probe_finished_nxdomain', 'the connection was reset',
            'took too long to respond', 'unexpectedly closed the connection',
        ]
        is_browser_error = any(sig in text_lower for sig in browser_error_signals)

        # Also check title for common error pages
        title_lower = title.lower() if title else ""
        is_error_page = is_browser_error or any(sig in title_lower for sig in [
            'page not found', '404 not found', '403 forbidden', '502 bad gateway',
            '503 service unavailable', '500 internal server error', 'error',
        ])

        paywall_signals = ['subscribe now', 'subscriber to access', 'sign in to read',
                           'subscription required', 'premium content', 'paywall',
                           'already a subscriber', 'this content is only available']
        has_paywall = any(sig in text_lower for sig in paywall_signals)
        # High ratio = lots of HTML but little text = content gated behind JS/paywall
        html_text_ratio = html_size / max(text_size, 1)
        likely_paywalled = has_paywall and (html_text_ratio > 50 or text_size < 1000)

        capture_failed = text_size < min_text_size or likely_paywalled or is_error_page
        wayback_used = False

        # -- Wayback Machine fallback (if direct capture failed for any reason) --
        wayback_snapshot_url = None
        if capture_failed:
            try:
                import urllib.request
                import urllib.error
                import socket

                # Check Wayback CDX API for available snapshots
                cdx_url = f"http://archive.org/wayback/available?url={url}"
                req = urllib.request.Request(cdx_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/145.0.0.0"
                })
                with urllib.request.urlopen(req, timeout=15) as resp:
                    cdx_data = json.loads(resp.read().decode("utf-8"))

                snapshot = cdx_data.get("archived_snapshots", {}).get("closest", {})
                wayback_snapshot_url = snapshot.get("url", "")

                if wayback_snapshot_url and snapshot.get("available"):
                    # Quick connectivity check -- web.archive.org may be blocked
                    can_reach_wayback = False
                    try:
                        sock = socket.create_connection(("web.archive.org", 443), timeout=5)
                        sock.close()
                        can_reach_wayback = True
                    except (socket.timeout, OSError):
                        pass

                    if can_reach_wayback:
                        # Snapshot exists and reachable -- re-fetch via nodriver
                        print(json.dumps({
                            "wayback_fallback": True,
                            "original_text_size": text_size,
                            "wayback_url": wayback_snapshot_url,
                        }))

                        old_stderr2 = sys.stderr
                        sys.stderr = open(os.devnull, 'w')
                        try:
                            wb_meta, wb_title, wb_final_url, wb_html_size, wb_text_size = asyncio.run(
                                archive_url(
                                    wayback_snapshot_url, entry_id=None, context=None, tags=None,
                                    wait_seconds=wait_seconds + 3,
                                    archives_dir=archives_dir, min_text_size=min_text_size
                                )
                            )
                        finally:
                            sys.stderr.close()
                            sys.stderr = old_stderr2

                        if wb_text_size >= min_text_size:
                            import shutil
                            wb_html = Path(wb_meta["html_file"])
                            wb_text = Path(wb_meta["text_file"])

                            html_path = Path(meta["html_file"])
                            text_path = Path(meta["text_file"])
                            shutil.copy2(wb_html, html_path)
                            shutil.copy2(wb_text, text_path)

                            try:
                                wb_html.unlink(missing_ok=True)
                                wb_text.unlink(missing_ok=True)
                                wb_meta_path = Path(str(wb_html).replace(".html", ".meta.json"))
                                wb_meta_path.unlink(missing_ok=True)
                            except Exception:
                                pass

                            text_size = wb_text_size
                            html_size = wb_html_size
                            title = wb_title or title
                            capture_failed = False
                            wayback_used = True

                            meta["text_size"] = text_size
                            meta["html_size"] = html_size
                            meta["title"] = title
                            meta["wayback_url"] = wayback_snapshot_url
                            meta["wayback_snapshot"] = snapshot.get("timestamp", "")
                            meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding='utf-8')
                    else:
                        print(json.dumps({
                            "wayback_available_but_unreachable": True,
                            "wayback_url": wayback_snapshot_url,
                            "hint": "web.archive.org is blocked from this machine. Access manually or fix firewall.",
                        }))
            except Exception as wb_err:
                print(json.dumps({
                    "wayback_fallback_error": str(wb_err),
                }))

        # Record request in shared rate limiter (blocked if capture failed with bot signals)
        parsed = urlparse(url)
        domain = parsed.netloc.replace("www.", "")
        rate_file = Path(BROWSE_RATE_FILE)
        record_request(domain, rate_file, was_blocked=capture_failed and text_size < 100)

        if capture_failed:
            # Determine failure reason from content heuristics
            # (text_content_raw and text_lower already read at validation gate)
            text_content = text_content_raw
            if is_browser_error:
                failure_reason = 'browser-error'
            elif is_error_page and not is_browser_error:
                failure_reason = 'http-error'
            elif likely_paywalled or any(w in text_lower for w in ['subscribe', 'paywall', 'premium', 'sign in to read']):
                failure_reason = 'paywall'
            elif text_size == 0:
                failure_reason = 'empty-page'
            elif any(w in text_lower for w in ['403', 'forbidden', 'access denied', 'blocked']):
                failure_reason = 'bot-blocked'
            elif final_url and urlparse(final_url).netloc != urlparse(url).netloc:
                failure_reason = 'redirect'
            elif html_size > 10000 and text_size < min_text_size:
                # Large HTML but thin text = content is JS-rendered (React/Vue/Angular SPA)
                # Agent should retry with WebFetch or Chrome DevTools MCP
                failure_reason = 'js-rendering'
            else:
                failure_reason = 'insufficient-content'
            archive_status = 'failed'
            capture_status = 'failed'
        else:
            failure_reason = None
            archive_status = 'captured-via-wayback' if wayback_used else 'captured'
            capture_status = 'captured'

        # Canonicalize URL for Source node MERGE (Phase 2 fix)
        canonical_url = canonicalize_url(url)
        original_url = url if canonical_url != url else None

        # Classify source type from domain (Phase 3)
        source_type = SOURCE_TYPE_MAP.get(domain, SOURCE_TYPE_MAP["_default"])

        # Wire into Neo4j
        driver = get_neo4j_driver()

        try:
            with driver.session(database=ENTRY_DATABASE) as session:
                # Create or update Source node -- with capture validation
                now = datetime.now(timezone.utc)
                session.run(
                    """MERGE (s:Source {url: $url})
                    ON CREATE SET
                        s.domain = $domain,
                        s.title = $title,
                        s.capturedAt = datetime($captured),
                        s.lastCaptured = datetime($captured),
                        s.captureCount = 1,
                        s.archivePath = $text_path,
                        s.htmlPath = $html_path,
                        s.textPath = $text_path,
                        s.metaPath = $meta_path,
                        s.urlHash = $hash,
                        s.tags = $tags,
                        s.archiveStatus = $archive_status,
                        s.captureStatus = $capture_status,
                        s.textSize = $text_size,
                        s.textPreview = $text_preview,
                        s.failureReason = $failure_reason,
                        s.waybackUrl = $wayback_url,
                        s.originalUrl = $original_url,
                        s.sourceType = $source_type,
                        s.author = $author,
                        s.publishedDate = $publishedDate,
                        s.siteName = $siteName
                    ON MATCH SET
                        s.lastCaptured = datetime($captured),
                        s.captureCount = COALESCE(s.captureCount, 0) + 1,
                        s.title = $title,
                        s.archiveStatus = $archive_status,
                        s.captureStatus = $capture_status,
                        s.textSize = $text_size,
                        s.textPreview = $text_preview,
                        s.failureReason = $failure_reason,
                        s.waybackUrl = COALESCE($wayback_url, s.waybackUrl),
                        s.originalUrl = COALESCE($original_url, s.originalUrl),
                        s.sourceType = COALESCE($source_type, s.sourceType),
                        s.author = COALESCE($author, s.author),
                        s.publishedDate = COALESCE($publishedDate, s.publishedDate),
                        s.siteName = COALESCE($siteName, s.siteName),
                        s.tags = CASE WHEN s.tags IS NULL THEN $tags
                                 ELSE [x IN s.tags WHERE NOT x IN $tags] + $tags END
                    """,
                    {
                        "url": canonical_url,
                        "domain": domain,
                        "title": title,
                        "captured": now.isoformat(),
                        "html_path": str(meta["html_file"]),
                        "text_path": str(meta["text_file"]),
                        "meta_path": str(meta.get("meta_file", meta["text_file"])),
                        "hash": meta["url_hash"],
                        "tags": tags,
                        "archive_status": archive_status,
                        "capture_status": capture_status,
                        "text_size": text_size,
                        "text_preview": text_content[:500] if capture_failed else meta.get("text_preview", "")[:500],
                        "failure_reason": failure_reason,
                        "wayback_url": meta.get("wayback_url") if wayback_used else None,
                        "original_url": original_url,
                        "source_type": source_type,
                        "author": meta.get("author") if not capture_failed else None,
                        "publishedDate": meta.get("publishedDate") if not capture_failed else None,
                        "siteName": meta.get("siteName") if not capture_failed else None,
                    }
                )

                # Wire CITES edge if entry_id provided
                if entry_id:
                    result = session.run(
                        """MATCH (e:StreamEntry {id: $entry_id}), (s:Source {url: $url})
                        MERGE (e)-[r:CITES]->(s)
                        SET r.context = $context, r.capturedAt = datetime($captured)
                        RETURN e.id, s.url""",
                        {
                            "entry_id": entry_id,
                            "url": canonical_url,
                            "context": context or "",
                            "captured": now.isoformat(),
                        }
                    )
                    cite_records = list(result)
                    if not cite_records:
                        print(f"WARNING: Entry {entry_id} not found -- Source created but CITES edge not wired", file=sys.stderr)

                # Create File node for the archived HTML
                html_path = Path(meta["html_file"])
                from lib.paths import USER_HOME as _USER_HOME
                rel_html = str(html_path).replace(str(_USER_HOME) + "\\", "").replace("\\", "/")
                date_str = now.strftime("%Y-%m-%d")
                session.run(
                    """MERGE (f:File {path: $path})
                    SET f.filename = $filename, f.fileType = 'html',
                        f.created = date($date)
                    WITH f
                    MATCH (s:Source {url: $url})
                    MERGE (s)-[:ARCHIVED_AS]->(f)""",
                    {
                        "path": rel_html,
                        "filename": html_path.name,
                        "date": date_str,
                        "url": canonical_url,
                    }
                )
            # Also write Source node to corcoran (knowledge graph needs sources for SUPPORTED_BY edges)
            with driver.session(database=GRAPH_DATABASE) as cor_session:
                cor_session.run(
                    """MERGE (s:Source {url: $url})
                    ON CREATE SET
                        s.domain = $domain,
                        s.title = $title,
                        s.capturedAt = datetime($captured),
                        s.lastCaptured = datetime($captured),
                        s.archiveStatus = $archive_status,
                        s.captureStatus = $capture_status,
                        s.textSize = $text_size,
                        s.textPreview = $text_preview,
                        s.archivePath = $text_path,
                        s.failureReason = $failure_reason,
                        s.waybackUrl = $wayback_url,
                        s.originalUrl = $original_url,
                        s.sourceType = $source_type,
                        s.author = $author,
                        s.publishedDate = $publishedDate,
                        s.siteName = $siteName
                    ON MATCH SET
                        s.lastCaptured = datetime($captured),
                        s.title = $title,
                        s.archiveStatus = $archive_status,
                        s.captureStatus = $capture_status,
                        s.textSize = $text_size,
                        s.textPreview = $text_preview,
                        s.archivePath = $text_path,
                        s.failureReason = $failure_reason,
                        s.waybackUrl = COALESCE($wayback_url, s.waybackUrl),
                        s.originalUrl = COALESCE($original_url, s.originalUrl),
                        s.sourceType = COALESCE($source_type, s.sourceType),
                        s.author = COALESCE($author, s.author),
                        s.publishedDate = COALESCE($publishedDate, s.publishedDate),
                        s.siteName = COALESCE($siteName, s.siteName)
                    """,
                    {
                        "url": canonical_url,
                        "domain": domain,
                        "title": title,
                        "captured": now.isoformat(),
                        "archive_status": archive_status,
                        "capture_status": capture_status,
                        "text_size": text_size,
                        "text_preview": text_content[:500] if capture_failed else meta.get("text_preview", "")[:500],
                        "text_path": str(meta["text_file"]),
                        "failure_reason": failure_reason,
                        "wayback_url": meta.get("wayback_url") if wayback_used else None,
                        "original_url": original_url,
                        "source_type": source_type,
                        "author": meta.get("author") if not capture_failed else None,
                        "publishedDate": meta.get("publishedDate") if not capture_failed else None,
                        "siteName": meta.get("siteName") if not capture_failed else None,
                    }
                )
        finally:
            driver.close()

        # -- Save Page Now (SPN) submission -- best effort --
        # Submit on success (preserve publicly) and on recoverable failures
        # Queue for SPN preservation (fire-and-forget, async worker handles submission)
        spn_skip_reasons = {'browser-error', 'http-error'}
        should_submit_spn = not capture_failed or (failure_reason not in spn_skip_reasons)
        spn_result = None
        if should_submit_spn:
            spn_result = enqueue_spn(url)

        result = {
            "status": archive_status,
            "url": canonical_url,
            "original_url": original_url,
            "title": title,
            "domain": domain,
            "captured_at": now.isoformat(),
            "html_size": html_size,
            "text_size": text_size,
            "html_path": str(meta["html_file"]),
            "text_path": str(meta["text_file"]),
            "meta_path": str(meta.get("meta_file", meta["text_file"])),
            "capture_method": capture_method,
            "neo4j": "Source node + ARCHIVED_AS edge created",
            "cites": entry_id or "none",
        }
        if wayback_used:
            result["wayback_fallback"] = True
            result["wayback_url"] = meta.get("wayback_url", "")
            result["status"] = "captured-via-wayback"
        if wayback_snapshot_url and not wayback_used:
            result["wayback_snapshot_available"] = wayback_snapshot_url
        if capture_failed:
            result["WARNING"] = f"CAPTURE FAILED: only {text_size} chars extracted (minimum: {min_text_size}). Source marked as failed (reason: {failure_reason}). Do NOT cite this source as archived-verified."
            result["failure_reason"] = failure_reason
            # Add agent hints for fallback strategies based on failure type
            if failure_reason == 'js-rendering':
                result["fallback_hint"] = "Content is JS-rendered. Try WebFetch first; if still thin, use Chrome DevTools MCP to get rendered text, then pass to save_page."
            elif failure_reason == 'bot-blocked':
                result["fallback_hint"] = "Site blocks automated access. Try Chrome DevTools MCP (different fingerprint) or note as manually-accessible only."
            elif failure_reason == 'paywall':
                result["fallback_hint"] = "Content behind paywall. Check Wayback Machine manually in Brave, or use save_page with text= parameter if content is available elsewhere."
        # Attach SPN result if we attempted submission
        if spn_result:
            result["spn"] = {
                "status": spn_result.get("status", "unknown"),
                "wayback_url": spn_result.get("wayback_url"),
                "detail": spn_result.get("detail", ""),
            }
        output(result)

    except Exception as e:
        import traceback
        output({
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
        sys.exit(1)


if __name__ == "__main__":
    main()
