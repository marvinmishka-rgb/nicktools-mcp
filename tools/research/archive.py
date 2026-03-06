"""Unified web archiver: auto, mhtml, and full capture modes.
---
description: Archive a URL with mode selection, Source node creation, and Wayback queueing
creates_nodes: [Source]
creates_edges: [CITES]
databases: [corcoran, lifestream]
---

Single entry point for "I want to archive a page." Replaces archive_source and save_page
with a single intent-based operation.

Three modes:
  - auto (default): Uses four-tier capture pipeline + save_capture() + Source node creation.
    Most reliable, handles 90%+ of URLs. Entirely in-process via lib/capture.py.
  - mhtml: Spawns save_page.py as subprocess for CDP Page.captureSnapshot. Returns full
    MHTML archive plus semantic extraction. Use when visual fidelity matters.
  - full: Spawns archive_source.py as subprocess for nodriver full-page capture with raw
    HTML preservation. Use when you need original HTML structure. Handles PDF URLs.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.io import setup_output, load_params, output
from lib.urls import canonicalize_url, extract_domain, SOURCE_TYPE_MAP
from lib.capture import capture_page, save_capture
from lib.spn import enqueue_spn
from lib.paths import ARCHIVES_DIR
from lib.db import GRAPH_DATABASE, ENTRY_DATABASE


PYTHON_EXE = r"C:\Python313\python.exe"
TOOLS_DIR = Path(__file__).parent


# ============================================================
# Mode: auto — in-process capture + archive + Source node
# ============================================================

def _archive_auto(url, tags=None, entry_id=None, context=None,
                  spn=True, timeout=15, min_text_size=None, driver=None):
    """Archive via four-tier capture pipeline + save_capture + Source node.

    This is the same path as read(archive=true) but focused on archiving.
    Returns dict with archive paths, Source node status, and content summary.
    """
    canonical = canonicalize_url(url)
    domain = extract_domain(canonical)

    # Capture content
    capture = capture_page(canonical, timeout=timeout)
    if not capture.get("success"):
        return {
            "success": False,
            "url": canonical,
            "domain": domain,
            "error": f"Capture failed: {capture.get('error', 'unknown')}",
            "tier_errors": capture.get("tier_errors", {}),
            "mode": "auto",
        }

    # Check minimum text threshold
    article_text = capture.get("article_text", "") or capture.get("text", "")
    from lib.capture import MIN_TEXT_SIZE
    threshold = min_text_size or MIN_TEXT_SIZE
    if len(article_text.strip()) < threshold:
        return {
            "success": False,
            "url": canonical,
            "domain": domain,
            "error": f"Content too short ({len(article_text)} chars, need {threshold})",
            "capture_method": capture.get("capture_method"),
            "mode": "auto",
        }

    # Save to archive files
    paths = save_capture(canonical, capture)

    # Create Source node in both databases
    if driver:
        _create_source_node(
            canonical, capture, paths, tags=tags, driver=driver
        )

    # Wire CITES edge if entry_id provided
    if entry_id and driver:
        _wire_cites(canonical, entry_id, driver)

    # SPN queueing
    spn_queued = False
    if spn:
        try:
            spn_result = enqueue_spn(canonical)
            spn_queued = spn_result.get("queued", False)
        except Exception:
            pass

    return {
        "success": True,
        "url": canonical,
        "domain": domain,
        "title": capture.get("title", ""),
        "capture_method": capture.get("capture_method", "unknown"),
        "text_size": len(article_text),
        "word_count": len(article_text.split()) if article_text else 0,
        "archive_paths": paths,
        "source_node": "created" if driver else "skipped (no driver)",
        "cites_edge": entry_id if entry_id else None,
        "spn_queued": spn_queued,
        "mode": "auto",
    }


# ============================================================
# Mode: mhtml — subprocess delegation to save_page.py
# ============================================================

def _archive_mhtml(url, tags=None, entry_id=None, context=None,
                   wait_seconds=8, spn=True, timeout=120):
    """Archive via CDP Page.captureSnapshot (full MHTML).

    Delegates to save_page.py as subprocess. save_page handles its own
    Source node creation, CITES wiring, and file saving.
    """
    save_page_script = TOOLS_DIR / "save_page.py"

    params = {
        "url": url,
        "wait_seconds": wait_seconds,
    }
    if entry_id:
        params["entry_id"] = entry_id
    if tags:
        params["tags"] = tags
    if context:
        params["context"] = context

    return _run_subprocess_tool(save_page_script, params, timeout, "mhtml")


# ============================================================
# Mode: full — subprocess delegation to archive_source.py
# ============================================================

def _archive_full(url, tags=None, entry_id=None, context=None,
                  wait_seconds=5, spn=True, timeout=120):
    """Archive via nodriver with raw HTML preservation.

    Delegates to archive_source.py as subprocess. archive_source handles
    its own Source node creation, CITES wiring, and file saving.
    Handles PDF URLs automatically.
    """
    archive_source_script = TOOLS_DIR / "archive_source.py"

    params = {
        "url": url,
        "wait_seconds": wait_seconds,
    }
    if entry_id:
        params["entry_id"] = entry_id
    if tags:
        params["tags"] = tags if isinstance(tags, list) else [t.strip() for t in tags.split(",")]
    if context:
        params["context"] = context

    return _run_subprocess_tool(archive_source_script, params, timeout, "full")


# ============================================================
# Shared subprocess runner
# ============================================================

def _run_subprocess_tool(script_path, params, timeout, mode_name):
    """Run a subprocess tool script and parse its JSON output.

    Uses the same temp-file param passing as the server dispatcher.
    Parses multi-line stdout to find JSON result objects.
    """
    params_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='_archive_params.json', delete=False, encoding='utf-8'
    )
    json.dump(params, params_file, ensure_ascii=False)
    params_file.close()

    try:
        proc = subprocess.run(
            [PYTHON_EXE, str(script_path), params_file.name],
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

        stdout = (proc.stdout or "").strip()

        if proc.returncode != 0:
            stderr_hint = (proc.stderr or "")[:300]
            return {
                "success": False,
                "error": f"Subprocess failed (exit {proc.returncode}): {stderr_hint}",
                "mode": mode_name,
            }

        if not stdout:
            return {
                "success": False,
                "error": "Subprocess returned empty output",
                "mode": mode_name,
            }

        # Parse last JSON object from stdout (subprocess tools may emit
        # multiple JSON objects: rate limit notices, progress, then result)
        result = _parse_last_json(stdout)
        if result is None:
            return {
                "success": False,
                "error": f"No valid JSON in output. First 300 chars: {stdout[:300]}",
                "mode": mode_name,
            }

        result["mode"] = mode_name
        # Normalize success field
        if "success" not in result:
            result["success"] = not result.get("error")
        return result

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": f"Archive ({mode_name}) timed out after {timeout}s",
            "mode": mode_name,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Archive ({mode_name}) error: {e}",
            "mode": mode_name,
        }
    finally:
        try:
            os.unlink(params_file.name)
        except Exception:
            pass


def _parse_last_json(text):
    """Extract the last valid JSON dict from a string.

    Handles nodriver cleanup messages and multi-JSON output.
    """
    decoder = json.JSONDecoder()
    result = None
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos] != '{':
            pos += 1
        if pos >= len(text):
            break
        try:
            obj, end_pos = decoder.raw_decode(text, pos)
            if isinstance(obj, dict):
                result = obj
            pos = end_pos
        except json.JSONDecodeError:
            pos += 1
    return result


# ============================================================
# Source node creation (reused from read.py pattern)
# ============================================================

def _create_source_node(url, capture, paths, tags=None, driver=None):
    """Create/update Source node in both corcoran and lifestream databases."""
    if not driver:
        return

    canonical = canonicalize_url(url)
    domain = extract_domain(canonical)
    metadata = capture.get("metadata", {})
    article_text = capture.get("article_text", "") or capture.get("text", "")
    now = datetime.now(timezone.utc)
    source_type = SOURCE_TYPE_MAP.get(domain, "web-article")

    params = {
        "url": canonical,
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


def _wire_cites(url, entry_id, driver):
    """Wire CITES edge from a StreamEntry to a Source node."""
    if not entry_id or not driver:
        return

    canonical = canonicalize_url(url)
    cites_cypher = """
        MATCH (e:StreamEntry {id: $entry_id})
        MATCH (s:Source {url: $url})
        MERGE (e)-[:CITES]->(s)
    """
    # CITES goes in lifestream (where StreamEntries live)
    with driver.session(database=ENTRY_DATABASE) as session:
        session.run(cites_cypher, {"entry_id": entry_id, "url": canonical})


# ============================================================
# Main archive implementation
# ============================================================

def archive_impl(url=None, mode="auto", tags=None, entry_id=None, context=None,
                 wait_seconds=None, spn=True, timeout=None, min_text_size=None,
                 driver=None, **kwargs):
    """Archive a web page with mode selection.

    Modes:
        auto (default): Four-tier capture pipeline. Most reliable, handles 90%+.
            Creates Source node in-process. Best for most archiving.
        mhtml: CDP Page.captureSnapshot for full MHTML archive. Use when
            visual fidelity matters. Delegates to save_page.py subprocess.
        full: Nodriver capture with raw HTML preservation. Handles PDF URLs
            automatically. Delegates to archive_source.py subprocess.

    Args:
        url: URL to archive (required)
        mode: "auto" (default), "mhtml", or "full"
        tags: Tags for Source node (list or CSV string)
        entry_id: Lifestream entry ID for CITES edge wiring
        context: Reason/context for archiving
        wait_seconds: JS render wait (mode-dependent defaults: auto=n/a, mhtml=8, full=5)
        spn: Queue URL for Wayback Machine SPN on success (default True)
        timeout: Per-operation timeout in seconds
        min_text_size: Minimum chars for successful extraction (auto mode only)
        driver: Neo4j driver (injected by dispatcher)

    Returns:
        dict with success, url, domain, title, mode, archive_paths or paths,
        source_node status, capture_method, text_size
    """
    if not url:
        return {"error": "Missing required parameter 'url'"}

    if mode not in ("auto", "mhtml", "full"):
        return {"error": f"Invalid mode '{mode}'. Must be: auto, mhtml, or full"}

    # Normalize tags
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    if mode == "auto":
        return _archive_auto(
            url, tags=tags, entry_id=entry_id, context=context,
            spn=spn, timeout=timeout or 15, min_text_size=min_text_size,
            driver=driver,
        )
    elif mode == "mhtml":
        return _archive_mhtml(
            url, tags=tags, entry_id=entry_id, context=context,
            wait_seconds=wait_seconds or 8, spn=spn, timeout=timeout or 120,
        )
    elif mode == "full":
        return _archive_full(
            url, tags=tags, entry_id=entry_id, context=context,
            wait_seconds=wait_seconds or 5, spn=spn, timeout=timeout or 120,
        )


# Subprocess entry point
if __name__ == "__main__":
    setup_output()
    params = load_params()
    # When running as subprocess, create own driver for auto mode (Source node creation).
    # mhtml/full modes delegate to their own subprocess tools which create their own drivers.
    mode = params.get("mode", "auto")
    if mode == "auto" and "driver" not in params:
        from lib.db import get_neo4j_driver
        driver = get_neo4j_driver()
        params["driver"] = driver
    result = archive_impl(**params)
    output(result)
    if mode == "auto" and "driver" in params:
        try:
            params["driver"].close()
        except Exception:
            pass
