"""
search_pdf -- Search and extract text from PDF documents.

Downloads PDFs from URLs or reads local files, extracts metadata,
searches for terms across pages, and optionally archives + creates
Source nodes. Uses pymupdf (PyMuPDF) for extraction.

Phase 4 of tool-upgrade-plan-v3.md.
---
description: Search/extract text from PDFs, optionally create Source node
creates_nodes: [Source]
databases: [corcoran]
---
"""
import sys
import json
import re
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, GRAPH_DATABASE, ENTRY_DATABASE
from lib.io import setup_output, load_params, output
from lib.paths import ARCHIVES_DIR, USER_HOME, ensure_dir
from lib.urls import extract_domain, canonicalize_url, SOURCE_TYPE_MAP


def _parse_page_ranges(spec):
    """Parse page range spec like '1-5,307,400-402' into a sorted list of 0-indexed page numbers."""
    pages = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            start, end = int(start.strip()), int(end.strip())
            pages.update(range(start - 1, end))  # convert to 0-indexed
        else:
            pages.add(int(part) - 1)  # convert to 0-indexed
    return sorted(pages)


def _download_pdf(url, archives_dir):
    """Download PDF from URL with anti-detection headers.

    Returns (local_path, file_size_bytes) or raises on failure.
    """
    import urllib.request

    domain = extract_domain(url)
    filename = url.split("/")[-1].split("?")[0]
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    # Sanitize filename for Windows (max 200 chars, no special chars)
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)[:200]

    output_dir = Path(archives_dir) / domain
    ensure_dir(output_dir, "PDF output directory")
    output_path = output_dir / filename

    # If already downloaded, return cached copy
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path, output_path.stat().st_size

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
        output_path.write_bytes(data)

    return output_path, len(data)


def search_pdf_impl(path, search_terms="", extract_pages="", archive=False,
                    context_chars=300, driver=None, **kwargs):
    """Search and extract text from a PDF document.

    Args:
        path: Local file path or URL to PDF
        search_terms: Comma-separated search terms
        extract_pages: Page range spec (e.g. '1-5,307')
        archive: If True, create Source node in Neo4j
        context_chars: Characters of context around matches
        driver: Optional shared Neo4j driver

    Returns:
        dict with metadata, matches, extracted_text, archive_info
    """
    import pymupdf

    result = {
        "metadata": {},
        "matches": [],
        "extracted_text": {},
        "archive_info": None,
        "warnings": [],
    }

    # Step 1: Resolve path (download if URL)
    local_path = None
    downloaded = False
    download_size = 0

    if path.startswith("http://") or path.startswith("https://"):
        try:
            local_path, download_size = _download_pdf(path, ARCHIVES_DIR)
            downloaded = True
            result["metadata"]["downloaded_from"] = path
            result["metadata"]["download_size"] = download_size
        except Exception as e:
            result["warnings"].append(f"Download failed: {e}")
            return result
    else:
        # Local path -- try as-is, then with Windows prefix
        candidates = [
            Path(path),
            Path(str(USER_HOME) + "\\" + path) if not Path(path).is_absolute() else None,
        ]
        for p in candidates:
            if p and p.exists():
                local_path = p
                break

        if not local_path:
            result["warnings"].append(f"File not found: {path}")
            return result

    # Step 2: Open with pymupdf and extract metadata
    try:
        doc = pymupdf.open(str(local_path))
    except Exception as e:
        result["warnings"].append(f"Failed to open PDF: {e}")
        return result

    meta = doc.metadata or {}
    result["metadata"].update({
        "path": str(local_path),
        "page_count": len(doc),
        "title": meta.get("title", ""),
        "author": meta.get("author", ""),
        "subject": meta.get("subject", ""),
        "creator": meta.get("creator", ""),
        "producer": meta.get("producer", ""),
        "creation_date": meta.get("creationDate", ""),
        "mod_date": meta.get("modDate", ""),
        "file_size": local_path.stat().st_size,
    })

    # Step 3: Search for terms across all pages
    if search_terms:
        terms = [t.strip() for t in search_terms.split(",") if t.strip()]

        for page_num in range(len(doc)):
            page = doc[page_num]
            text = page.get_text()
            if not text:
                continue

            text_lower = text.lower()
            for term in terms:
                term_lower = term.lower()
                # Find all occurrences in this page
                start = 0
                while True:
                    idx = text_lower.find(term_lower, start)
                    if idx == -1:
                        break

                    # Extract context
                    ctx_start = max(0, idx - context_chars)
                    ctx_end = min(len(text), idx + len(term) + context_chars)
                    context = text[ctx_start:ctx_end].strip()

                    # Add ellipsis markers
                    if ctx_start > 0:
                        context = "..." + context
                    if ctx_end < len(text):
                        context = context + "..."

                    result["matches"].append({
                        "term": term,
                        "page": page_num + 1,  # 1-indexed for human display
                        "position": idx,
                        "context": context,
                    })

                    start = idx + len(term)

        result["metadata"]["total_matches"] = len(result["matches"])
        result["metadata"]["terms_searched"] = terms

    # Step 4: Extract specific pages
    if extract_pages:
        page_indices = _parse_page_ranges(extract_pages)
        for idx in page_indices:
            if 0 <= idx < len(doc):
                page = doc[idx]
                text = page.get_text()
                result["extracted_text"][str(idx + 1)] = text  # 1-indexed key
            else:
                result["warnings"].append(f"Page {idx + 1} out of range (document has {len(doc)} pages)")

    # Step 5: Archive -- create Source node in Neo4j
    if archive:
        _driver = driver or get_neo4j_driver()
        try:
            source_url = path if path.startswith("http") else f"file:///{str(local_path).replace(chr(92), '/')}"
            canonical = canonicalize_url(source_url) if path.startswith("http") else source_url
            domain = extract_domain(source_url) if path.startswith("http") else "local"
            source_type = SOURCE_TYPE_MAP.get(domain, SOURCE_TYPE_MAP["_default"])
            now = datetime.now(timezone.utc)

            for db_name in [GRAPH_DATABASE, ENTRY_DATABASE]:
                with _driver.session(database=db_name) as session:
                    session.run(
                        """MERGE (s:Source {url: $url})
                        ON CREATE SET
                            s.domain = $domain,
                            s.title = $title,
                            s.archiveStatus = 'captured',
                            s.capturedAt = datetime($captured),
                            s.lastCaptured = datetime($captured),
                            s.archivePath = $archive_path,
                            s.sourceType = $source_type,
                            s.textSize = $page_count,
                            s.captureMethod = 'search_pdf'
                        ON MATCH SET
                            s.lastCaptured = datetime($captured),
                            s.sourceType = COALESCE($source_type, s.sourceType)
                        """,
                        {
                            "url": canonical,
                            "domain": domain,
                            "title": result["metadata"].get("title") or local_path.stem,
                            "captured": now.isoformat(),
                            "archive_path": str(local_path),
                            "source_type": source_type,
                            "page_count": len(doc),
                        }
                    )

            result["archive_info"] = {
                "status": "archived",
                "url": canonical,
                "domain": domain,
                "source_type": source_type,
                "databases": [GRAPH_DATABASE, ENTRY_DATABASE],
            }
        except Exception as e:
            result["warnings"].append(f"Archive failed: {e}")
        finally:
            if not driver:
                _driver.close()

    doc.close()

    # Truncate extracted_text if very large (for JSON output safety)
    for page_key in result["extracted_text"]:
        if len(result["extracted_text"][page_key]) > 10000:
            result["extracted_text"][page_key] = result["extracted_text"][page_key][:10000] + "\n... [truncated]"

    # Limit matches output to 50 for readability
    if len(result["matches"]) > 50:
        total = len(result["matches"])
        result["matches"] = result["matches"][:50]
        result["warnings"].append(f"Showing first 50 of {total} matches")

    return result


# Subprocess entry point
if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = search_pdf_impl(
        path=p.get("path", ""),
        search_terms=p.get("search_terms", ""),
        extract_pages=p.get("extract_pages", ""),
        archive=p.get("archive", False),
        context_chars=p.get("context_chars", 300),
    )
    output(r)
