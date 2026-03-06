"""
extract_saved_article -- Extract article text from saved HTML files.

(Renamed from extract_article for clarity — this operates on locally-saved
HTML files, not live URLs.)

Handles "Save Page Complete" downloads from browsers (HTML + _files/ folder).
Strips JavaScript paywall enforcement to recover full article text that was
delivered to the browser but hidden by client-side JS.

Three-tier content extraction:
  1. JSON-LD articleBody (structured, cleanest)
  2. <article> or <main> semantic container
  3. All <p> tags with paywall-class detection (broadest fallback)

Archives both the original saved page bundle AND extracted text.
Registers Source nodes in the corcoran Neo4j database.

Designed as in-process tool (_impl function) for fast dispatch.
---
description: Extract article text from saved HTML, strip JS paywall
creates_nodes: [Source]
creates_edges: [CITES]
databases: [corcoran, lifestream]
---
"""
import json
import os
import re
import shutil
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.paths import ARCHIVES_DIR, ensure_dir
from lib.entries import normalize_path
from lib.urls import extract_domain, canonicalize_url
from lib.db import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, GRAPH_DATABASE, ENTRY_DATABASE

# Windows extended-length path prefix
WIN_LONG = "\\\\?\\"


def _long_path(p):
    """Add Windows extended-length prefix for paths > 240 chars."""
    s = str(p)
    if len(s) > 240 and not s.startswith(WIN_LONG):
        return WIN_LONG + s
    return s


def _find_html_file(path):
    """Find the main HTML file from a path (file or folder).

    Handles three input patterns:
      1. Direct path to an .html file
      2. Path to a _files/ folder (companion HTML is sibling or inside)
      3. Path to a folder containing an .html file
    """
    p = Path(path)

    # Pattern 1: Direct HTML file
    if p.suffix.lower() in ('.html', '.htm') and os.path.isfile(_long_path(p)):
        return p

    # Pattern 2: _files folder -- look for sibling HTML or HTML inside
    if p.is_dir() or str(p).endswith('_files'):
        # Check inside the folder
        try:
            for entry in os.scandir(_long_path(p)):
                if entry.name.endswith('.html') or entry.name.endswith('.htm'):
                    return Path(entry.path)
        except OSError:
            pass

        # Check sibling (folder named X_files -> look for X.html)
        if str(p).endswith('_files'):
            sibling = Path(str(p).replace('_files', '.html'))
            if os.path.isfile(_long_path(sibling)):
                return sibling

    # Pattern 3: Generic folder -- find first .html
    if p.is_dir():
        try:
            for entry in os.scandir(_long_path(p)):
                if entry.name.endswith('.html'):
                    return Path(entry.path)
        except OSError:
            pass

    return None


def _read_html(html_path):
    """Read HTML file using extended-length path if needed."""
    with open(_long_path(html_path), 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def _extract_metadata(soup, html):
    """Extract article metadata from HTML."""
    meta = {}

    # Title
    og_title = soup.find('meta', property='og:title')
    if og_title:
        meta['title'] = og_title.get('content', '')
    elif soup.title:
        meta['title'] = soup.title.get_text(strip=True)

    # Author
    for selector in [
        ('meta', {'name': 'author'}),
        ('meta', {'property': 'article:author'}),
        ('meta', {'name': 'dc.creator'}),
    ]:
        tag = soup.find(*selector)
        if tag and tag.get('content'):
            meta['author'] = tag['content']
            break

    # Published date
    for selector in [
        ('meta', {'property': 'article:published_time'}),
        ('meta', {'name': 'date'}),
        ('meta', {'name': 'dc.date'}),
        ('time', {'class': lambda c: c and 'publish' in str(c).lower()}),
    ]:
        tag = soup.find(*selector)
        if tag:
            val = tag.get('content') or tag.get('datetime') or tag.get_text(strip=True)
            if val:
                meta['published_date'] = val[:10]  # YYYY-MM-DD
                break

    # URL (canonical or og:url)
    canonical = soup.find('link', rel='canonical')
    if canonical:
        meta['url'] = canonical.get('href', '')
    else:
        og_url = soup.find('meta', property='og:url')
        if og_url:
            meta['url'] = og_url.get('content', '')

    # Site name
    og_site = soup.find('meta', property='og:site_name')
    if og_site:
        meta['site_name'] = og_site.get('content', '')

    # Unwrap Wayback Machine URLs to get the original
    if meta.get('url') and 'web.archive.org/web/' in meta['url']:
        # Pattern: https://web.archive.org/web/20250225124125/https://www.example.com/...
        wb_match = re.search(r'web\.archive\.org/web/\d+/(https?://.+)', meta['url'])
        if wb_match:
            meta['wayback_url'] = meta['url']  # preserve the Wayback URL
            meta['url'] = wb_match.group(1)

    return meta


def _detect_paywall_pattern(soup, html):
    """Detect which paywall enforcement pattern the page uses."""
    patterns = []

    # Class-based paywall markers
    paywall_classes = soup.find_all(class_=lambda c: c and 'paywall' in str(c).lower())
    if paywall_classes:
        classes = set()
        for el in paywall_classes:
            for c in el.get('class', []):
                if 'paywall' in c.lower():
                    classes.add(c)
        patterns.append(f"class-based: {', '.join(classes)} ({len(paywall_classes)} elements)")

    # Regwall / registration wall
    regwall = soup.find_all(class_=lambda c: c and 'regwall' in str(c).lower())
    if regwall:
        patterns.append(f"regwall: {len(regwall)} elements")

    # Piano / Tinypass (common paywall vendor)
    if 'piano' in html.lower() or 'tinypass' in html.lower():
        patterns.append("Piano/Tinypass paywall vendor detected")

    # Subscriber-only content markers
    sub_markers = soup.find_all(class_=lambda c: c and any(
        kw in str(c).lower() for kw in ['subscriber', 'premium', 'locked', 'gated']
    ))
    if sub_markers:
        patterns.append(f"subscriber/premium markers: {len(sub_markers)} elements")

    # Sign-in overlay
    signin = soup.find_all(class_=lambda c: c and any(
        kw in str(c).lower() for kw in ['signin-wall', 'login-wall', 'barrier', 'gate-overlay']
    ))
    if signin:
        patterns.append(f"sign-in/barrier overlay: {len(signin)} elements")

    return patterns


def _extract_text(soup, html):
    """Extract article text using three-tier strategy.

    Returns (text, method, paragraph_count).
    """
    # Tier 1: JSON-LD articleBody
    schema_match = re.search(r'"articleBody"\s*:\s*"((?:[^"\\]|\\.)*)"', html)
    if schema_match:
        body = schema_match.group(1)
        # Unescape JSON
        body = body.encode().decode('unicode_escape', errors='replace')
        if len(body) > 500:
            paragraphs = [p.strip() for p in body.split('\n') if p.strip()]
            return '\n\n'.join(paragraphs), 'json-ld-articleBody', len(paragraphs)

    # Tier 2: Semantic container (<article> or <main>)
    container = soup.find('article') or soup.find('main')
    if container:
        paragraphs = []
        for p in container.find_all('p'):
            text = p.get_text(strip=True)
            if len(text) > 15:
                paragraphs.append(text)
        if len(paragraphs) >= 3:
            return '\n\n'.join(paragraphs), 'semantic-container', len(paragraphs)

    # Tier 3: All <p> tags (broadest fallback)
    paragraphs = []
    for p in soup.find_all('p'):
        text = p.get_text(strip=True)
        if len(text) > 20:
            paragraphs.append(text)
    if paragraphs:
        return '\n\n'.join(paragraphs), 'all-paragraphs', len(paragraphs)

    return '', 'none', 0


def _archive_bundle(html_path, domain, url_hash):
    """Copy saved page bundle to archives with short filenames.

    Handles:
      - HTML file -> archives/{domain}/{date}_{hash}.html
      - _files/ companion folder -> archives/{domain}/{date}_{hash}_files/
    """
    date_str = datetime.now().strftime("%Y-%m-%d")
    dest_dir = ARCHIVES_DIR / domain
    ensure_dir(dest_dir, "article extraction directory")

    base_name = f"{date_str}_{url_hash}"

    # Copy HTML file
    dest_html = dest_dir / f"{base_name}.html"
    shutil.copy2(_long_path(html_path), str(dest_html))
    archived_paths = {"html": str(dest_html)}

    # Look for _files companion folder
    html_stem = html_path.stem
    files_dir = html_path.parent / f"{html_stem}_files"
    if not os.path.isdir(_long_path(files_dir)):
        # Also check if the HTML is inside a _files folder
        if html_path.parent.name.endswith('_files'):
            files_dir = html_path.parent

    if os.path.isdir(_long_path(files_dir)):
        dest_files = dest_dir / f"{base_name}_files"
        if dest_files.exists():
            shutil.rmtree(_long_path(str(dest_files)))
        # Copy file-by-file with short destination names to avoid path length issues
        ensure_dir(dest_files, "article files directory")
        copied = 0
        for entry in os.scandir(_long_path(files_dir)):
            # Truncate long filenames to stay under path limit
            name = entry.name
            if len(str(dest_files / name)) > 240:
                ext = Path(name).suffix
                name = hashlib.md5(name.encode()).hexdigest()[:16] + ext
            try:
                shutil.copy2(entry.path, str(dest_files / name))
                copied += 1
            except OSError:
                pass  # Skip files that still exceed limits
        archived_paths["files_dir"] = str(dest_files)
        archived_paths["files_copied"] = copied

    return archived_paths


def extract_saved_article_impl(path, url=None, entry_id=None, context=None,
                               tags=None, archives_dir=None, driver=None, **kwargs):
    """Extract article text from a saved HTML file.

    Args:
        path: Path to HTML file or folder containing saved page
        url: Original URL of the article (auto-detected from HTML if omitted)
        entry_id: Optional lifestream entry ID for CITES edge
        context: Why this source matters
        tags: List of tags for the Source node
        archives_dir: Override for ARCHIVES_DIR
        driver: Shared Neo4j driver (injected by server)

    Returns:
        dict with extracted text, metadata, archive paths, and Source node status
    """
    from bs4 import BeautifulSoup

    tags = tags or []
    if archives_dir:
        global ARCHIVES_DIR
        ARCHIVES_DIR = Path(archives_dir)

    result = {
        "status": "error",
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }

    # -- Step 1: Find HTML file --
    html_path = _find_html_file(path)
    if not html_path:
        result["error"] = f"No HTML file found at: {path}"
        return result

    result["html_source"] = str(html_path)

    # -- Step 2: Read and parse HTML --
    try:
        html = _read_html(html_path)
    except Exception as e:
        result["error"] = f"Failed to read HTML: {e}"
        return result

    result["html_size"] = len(html)

    soup = BeautifulSoup(html, 'lxml')

    # Remove script/style/noscript/iframe before extraction
    for tag in soup(['script', 'style', 'noscript', 'iframe']):
        tag.decompose()

    # -- Step 3: Extract metadata --
    meta = _extract_metadata(soup, html)
    result["metadata"] = meta

    # Use auto-detected URL if none provided
    if not url:
        url = meta.get('url', '')
    result["url"] = url

    domain = extract_domain(url) if url else 'unknown'
    result["domain"] = domain

    # -- Step 4: Detect paywall pattern --
    # Re-parse original HTML (before decompose) for paywall detection
    soup_full = BeautifulSoup(html, 'lxml')
    paywall_patterns = _detect_paywall_pattern(soup_full, html)
    result["paywall_patterns"] = paywall_patterns

    # -- Step 5: Extract article text --
    text, method, para_count = _extract_text(soup, html)
    result["extraction_method"] = method
    result["paragraph_count"] = para_count
    result["text_size"] = len(text)

    if not text or len(text) < 100:
        result["status"] = "extraction-failed"
        result["error"] = "Insufficient article text extracted"
        return result

    # -- Step 6: Archive the bundle --
    url_hash = hashlib.md5((url or str(html_path)).encode()).hexdigest()[:12]

    try:
        archived = _archive_bundle(html_path, domain, url_hash)
        result["archived"] = archived
    except Exception as e:
        result["archive_error"] = f"Failed to copy bundle: {e}"
        archived = {}

    # Save extracted text
    date_str = datetime.now().strftime("%Y-%m-%d")
    text_filename = f"{date_str}_{url_hash}_extracted.txt"
    text_path = ARCHIVES_DIR / domain / text_filename

    header = []
    header.append(meta.get('title', 'Untitled'))
    if meta.get('author'):
        header.append(f"By {meta['author']}")
    if meta.get('site_name'):
        header.append(f"Source: {meta['site_name']}")
    if meta.get('published_date'):
        header.append(f"Published: {meta['published_date']}")
    if url:
        header.append(f"URL: {url}")
    header.append(f"Extracted: {date_str} via extract_article (stripped JS paywall)")
    if paywall_patterns:
        header.append(f"Paywall detected: {'; '.join(paywall_patterns)}")
    header.append("=" * 80)

    full_output = '\n'.join(header) + '\n\n' + text

    try:
        ensure_dir(text_path.parent, "parent directory for text_path")
        text_path.write_text(full_output, encoding='utf-8')
        result["text_file"] = str(text_path)
        archived["text"] = str(text_path)
    except Exception as e:
        result["text_file_error"] = f"Failed to write text: {e}"

    # -- Step 7: Neo4j Source node --
    # Canonicalize URL to prevent www./trailing-slash duplicates
    if url:
        url = canonicalize_url(url)
    if url and driver:
        try:
            with driver.session(database=GRAPH_DATABASE) as session:
                session.run(
                    "MERGE (s:Source {url: $url}) "
                    "ON CREATE SET s.domain = $domain, s.createdAt = datetime() "
                    "SET s.archiveStatus = 'captured', "
                    "    s.captureMethod = 'browser-save-extracted', "
                    "    s.title = $title, "
                    "    s.author = $author, "
                    "    s.publishedDate = $pubdate, "
                    "    s.textFile = $textFile, "
                    "    s.htmlFile = $htmlFile, "
                    "    s.capturedAt = datetime(), "
                    "    s.context = $context, "
                    "    s.tags = $tags, "
                    "    s.paywallPatterns = $patterns, "
                    "    s.extractionMethod = $method, "
                    "    s.failureReason = null ",
                    {
                        "url": url,
                        "domain": domain,
                        "title": meta.get('title', ''),
                        "author": meta.get('author', ''),
                        "pubdate": meta.get('published_date', ''),
                        "textFile": normalize_path(text_path) if text_path.exists() else '',
                        "htmlFile": normalize_path(archived.get('html', '')) if archived.get('html') else '',
                        "context": context or '',
                        "tags": tags,
                        "patterns": paywall_patterns,
                        "method": method,
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
                        {"url": url, "eid": entry_id, "context": context or ''}
                    )
                    # Wire in lifestream database too
                    with driver.session(database=ENTRY_DATABASE) as ls_session:
                        ls_session.run(
                            "MATCH (e:StreamEntry {id: $eid}) "
                            "MERGE (s:Source {url: $url}) "
                            "ON CREATE SET s.domain = $domain "
                            "MERGE (e)-[r:CITES]->(s) "
                            "SET r.context = $context",
                            {"url": url, "eid": entry_id, "domain": domain, "context": context or ''}
                        )

            result["neo4j"] = "Source node created/updated"
            if entry_id:
                result["cites"] = entry_id
        except Exception as e:
            result["neo4j_error"] = str(e)
    elif not url:
        result["neo4j"] = "skipped (no URL detected)"
    elif not driver:
        result["neo4j"] = "skipped (no driver)"

    result["status"] = "captured"
    return result


# -- Subprocess entry point --
if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    p = load_params()
    result = extract_saved_article_impl(**p)
    output(result)
