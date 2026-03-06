"""Process archive queue items with three-tier capture fallback.
---
description: Process queued URLs with HTTP+readability, Chrome CLI, and Wayback fallback
creates_nodes: [Source]
creates_edges: [CITES, ARCHIVED_AS]
databases: [corcoran, lifestream]
---

Replaces the fire-and-forget archive_worker.py with Claude-initiated,
observable queue processing. Each capture result is returned in real-time.

Capture tiers (tried in order):
  1. requests + readability-lxml  (~2s, 80%+ success rate)
  2. Chrome CLI --dump-dom        (~10s, for JS-rendered pages)
  3. Wayback CDX API              (~5s, for blocked/dead pages)

Source nodes are created atomically on capture success.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from lib.db import get_neo4j_driver, ENTRY_DATABASE, GRAPH_DATABASE
from lib.io import setup_output, load_params, output
from lib.paths import ARCHIVE_QUEUE_DIR, ARCHIVE_QUEUE_FILE, ARCHIVE_STAGED_DIR, ARCHIVES_DIR, ensure_dir
from lib.urls import canonicalize_url, extract_domain, SOURCE_TYPE_MAP
from lib.capture import capture_page, save_capture, MIN_TEXT_SIZE
from lib.browsing import enforce_rate_limit, record_request, BROWSE_RATE_FILE
from lib.spn import enqueue_spn


def process_queue_impl(batch_size=5, skip_failed=False, retry_failed=False,
                       min_delay=3, driver=None):
    """Process queued archive items with three-tier capture fallback.

    Args:
        batch_size: Max items to process per call (default 5)
        skip_failed: Skip items that have failed before (default False)
        retry_failed: Re-attempt items with status='failed' (default False)
        min_delay: Minimum seconds between same-domain requests (default 3)
        driver: Neo4j driver (auto-created if None)

    Returns:
        dict with processed, succeeded, failed, results, failures
    """
    # Load queue
    queue = _load_queue()
    if not queue:
        return {"processed": 0, "succeeded": 0, "failed": 0,
                "message": "Queue is empty", "results": [], "failures": []}

    # Filter items to process
    target_statuses = ["queued"]
    if retry_failed:
        target_statuses.append("failed")

    candidates = [
        item for item in queue
        if item["status"] in target_statuses
        and (not skip_failed or item.get("attempts", 0) == 0)
    ]

    if not candidates:
        return {"processed": 0, "succeeded": 0, "failed": 0,
                "message": "No items to process", "results": [], "failures": []}

    # Sort by priority
    priority_order = {"high": 0, "normal": 1, "low": 2}
    candidates.sort(key=lambda x: priority_order.get(x.get("priority", "normal"), 1))
    to_process = candidates[:batch_size]

    # Get Neo4j driver
    if driver is None:
        driver = get_neo4j_driver()
    own_driver = driver is not None

    # Register task for visibility
    task_id = None
    try:
        from lib.task_tracker import register_task, update_task as _update_task
        task_id = register_task(
            "process_queue",
            params={"batch_size": batch_size, "retry_failed": retry_failed},
            description=f"Archive queue: {len(to_process)} URLs",
            batch_size=len(to_process),
        )
    except Exception:
        pass

    results = []
    failures = []

    for item in to_process:
        url = item["url"]
        queue_id = item["queue_id"]
        domain = extract_domain(url)

        # Mark as processing
        item["status"] = "processing"
        _save_queue(queue)

        try:
            # Rate limit per domain
            enforce_rate_limit(domain, min_delay=min_delay)

            # Three-tier capture
            capture = capture_page(url)

            if not capture["success"]:
                # Capture failed across all tiers
                item["status"] = "failed"
                item["last_error"] = capture.get("error", "unknown")[:500]
                item["attempts"] = item.get("attempts", 0) + 1
                item["tier_errors"] = capture.get("tier_errors", {})
                _save_queue(queue)

                record_request(domain, was_blocked=True)

                failures.append({
                    "url": url,
                    "queue_id": queue_id,
                    "error": capture.get("error", "unknown"),
                    "tier_errors": capture.get("tier_errors", {}),
                    "attempts": item["attempts"],
                })
                # Update task progress mid-batch
                if task_id:
                    try:
                        _update_task(task_id, items_completed=len(results),
                                     items_failed=len(failures),
                                     error=f"{domain}: {capture.get('error', 'unknown')[:100]}")
                    except Exception:
                        pass
                continue

            # Save to filesystem
            paths = save_capture(url, capture)

            # Create Source nodes in Neo4j
            _create_source_nodes(
                driver, url, capture, paths,
                entry_id=item.get("entry_id"),
                tags=item.get("tags", []),
                context=item.get("context"),
            )

            # Queue for Wayback Machine SPN preservation (fire-and-forget)
            enqueue_spn(url)

            # Mark completed
            now = datetime.now(timezone.utc)
            item["status"] = "completed"
            item["completed_at"] = now.isoformat()
            item["capture_method"] = capture["capture_method"]
            item["attempts"] = item.get("attempts", 0) + 1
            _save_queue(queue)

            record_request(domain, was_blocked=False)

            article_text = capture.get("article_text", "")
            results.append({
                "url": url,
                "queue_id": queue_id,
                "status": "completed",
                "title": capture.get("title", "")[:120],
                "text_size": len(article_text),
                "word_count": len(article_text.split()),
                "capture_method": capture["capture_method"],
                "domain": domain,
                "article_path": paths.get("article_text_path"),
                "tier_errors": capture.get("tier_errors", {}),
            })
            # Update task progress mid-batch
            if task_id:
                try:
                    _update_task(task_id, items_completed=len(results),
                                 items_failed=len(failures))
                except Exception:
                    pass

        except Exception as e:
            item["status"] = "failed"
            item["last_error"] = str(e)[:500]
            item["attempts"] = item.get("attempts", 0) + 1
            _save_queue(queue)

            failures.append({
                "url": url,
                "queue_id": queue_id,
                "error": str(e)[:200],
                "attempts": item["attempts"],
            })

    # Finalize task tracker
    if task_id:
        try:
            final_status = "completed" if not failures else ("partial" if results else "failed")
            _update_task(
                task_id,
                status=final_status,
                items_completed=len(results),
                items_failed=len(failures),
                result_summary=f"succeeded={len(results)}, failed={len(failures)}",
            )
        except Exception:
            pass

    return {
        "processed": len(to_process),
        "succeeded": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
        "task_id": task_id,
    }


def _create_source_nodes(driver, url, capture, paths, entry_id=None, tags=None, context=None):
    """Create/update Source node after successful capture.

    Runs against BOTH corcoran and lifestream databases.
    Follows the pattern from archive_source.py.
    """
    canonical_url = canonicalize_url(url)
    original_url = url if canonical_url != url else None
    domain = extract_domain(url)
    now = datetime.now(timezone.utc)
    source_type = SOURCE_TYPE_MAP.get(domain, SOURCE_TYPE_MAP.get("_default", "article"))

    article_text = capture.get("article_text", "")
    metadata = capture.get("metadata", {})

    params = {
        "url": canonical_url,
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
        "original_url": original_url,
        "source_type": source_type,
        "author": metadata.get("author"),
        "published_date": metadata.get("published_date"),
        "site_name": metadata.get("site_name"),
        "tags": tags or [],
        "wayback_url": capture.get("wayback_url"),
    }

    source_cypher = """
        MERGE (s:Source {url: $url})
        ON CREATE SET
            s.domain = $domain,
            s.title = $title,
            s.capturedAt = datetime($captured),
            s.lastCaptured = datetime($captured),
            s.captureCount = 1,
            s.archiveStatus = $archive_status,
            s.captureStatus = $capture_status,
            s.captureMethod = $capture_method,
            s.textSize = $text_size,
            s.textPreview = $text_preview,
            s.archivePath = $text_path,
            s.htmlPath = $html_path,
            s.originalUrl = $original_url,
            s.sourceType = $source_type,
            s.author = $author,
            s.publishedDate = $published_date,
            s.siteName = $site_name,
            s.tags = $tags,
            s.waybackUrl = $wayback_url
        ON MATCH SET
            s.lastCaptured = datetime($captured),
            s.captureCount = COALESCE(s.captureCount, 0) + 1,
            s.title = CASE WHEN $title <> 'Untitled' THEN $title ELSE s.title END,
            s.archiveStatus = $archive_status,
            s.captureStatus = $capture_status,
            s.captureMethod = $capture_method,
            s.textSize = $text_size,
            s.textPreview = $text_preview,
            s.archivePath = $text_path,
            s.htmlPath = $html_path,
            s.originalUrl = COALESCE($original_url, s.originalUrl),
            s.sourceType = COALESCE($source_type, s.sourceType),
            s.author = COALESCE($author, s.author),
            s.publishedDate = COALESCE($published_date, s.publishedDate),
            s.siteName = COALESCE($site_name, s.siteName),
            s.waybackUrl = COALESCE($wayback_url, s.waybackUrl),
            s.tags = CASE WHEN s.tags IS NULL THEN $tags
                     ELSE [x IN s.tags WHERE NOT x IN $tags] + $tags END
    """

    for db_name in [GRAPH_DATABASE, ENTRY_DATABASE]:
        with driver.session(database=db_name) as session:
            session.run(source_cypher, params)

            # Wire CITES edge if entry_id provided (lifestream only)
            if entry_id and db_name == ENTRY_DATABASE:
                session.run(
                    """MATCH (e:StreamEntry {id: $entry_id}), (s:Source {url: $url})
                    MERGE (e)-[r:CITES]->(s)
                    SET r.context = $context, r.capturedAt = datetime($captured)""",
                    {"entry_id": entry_id, "url": canonical_url,
                     "context": context or "", "captured": now.isoformat()}
                )

            # Create File node for archived HTML (lifestream only)
            if paths.get("html_path") and db_name == ENTRY_DATABASE:
                html_path = Path(paths["html_path"])
                from lib.paths import USER_HOME as _USER_HOME
                rel_html = str(html_path).replace(str(_USER_HOME) + "\\", "").replace("\\", "/")
                session.run(
                    """MERGE (f:File {path: $path})
                    SET f.filename = $filename, f.fileType = 'html',
                        f.created = date($date)
                    WITH f
                    MATCH (s:Source {url: $url})
                    MERGE (s)-[:ARCHIVED_AS]->(f)""",
                    {"path": rel_html, "filename": html_path.name,
                     "date": now.strftime("%Y-%m-%d"), "url": canonical_url}
                )


def _load_queue():
    """Load queue from disk."""
    if ARCHIVE_QUEUE_FILE.exists():
        try:
            return json.loads(ARCHIVE_QUEUE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return []
    return []


def _save_queue(queue):
    """Save queue atomically."""
    ensure_dir(ARCHIVE_QUEUE_DIR, "archive queue directory")
    temp = ARCHIVE_QUEUE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(ARCHIVE_QUEUE_FILE)


if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = process_queue_impl(**params)
    output(result)
