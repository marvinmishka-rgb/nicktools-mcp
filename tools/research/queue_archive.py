"""Submit a URL to the async archive queue for background capture.
---
description: Queue a URL for async archiving (instant, non-blocking)
creates_nodes: []
creates_edges: []
databases: []
---

Adds a URL to the archive queue file (queue.json). A background worker
processes queued URLs asynchronously -- running Chrome capture, text
extraction, metadata parsing, and SPN submission. Results are written
to the staged/ directory as structured JSON.

This operation returns immediately. Use check_queue to poll status.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from lib.io import setup_output, load_params, output
from lib.paths import ARCHIVE_QUEUE_DIR, ARCHIVE_QUEUE_FILE, ARCHIVE_STAGED_DIR, ensure_dir
from lib.urls import canonicalize_url
from lib.spn import enqueue_spn


def queue_archive_impl(url, priority="normal", entry_id=None, context=None,
                       tags=None, wait_seconds=5, **kwargs):
    """Submit a URL to the archive queue.

    Args:
        url: The URL to archive
        priority: "high", "normal", or "low" (affects processing order)
        entry_id: Optional lifestream entry to wire CITES edge
        context: Context note for the archive
        tags: Tags for the Source node
        wait_seconds: Page load wait time for the capture worker

    Returns:
        dict with queue_id, position, url, status
    """
    if not url:
        return {"error": "Missing 'url' parameter"}

    # Ensure queue directory exists
    ensure_dir(ARCHIVE_QUEUE_DIR, "archive queue directory")
    ensure_dir(ARCHIVE_STAGED_DIR, "archive staged directory")

    # Canonicalize URL
    canonical_url = canonicalize_url(url)
    original_url = url if canonical_url != url else None

    # Generate queue ID from URL hash + timestamp
    now = datetime.now(timezone.utc)
    url_hash = hashlib.sha256(canonical_url.encode()).hexdigest()[:12]
    queue_id = f"{now.strftime('%Y%m%d_%H%M%S')}_{url_hash}"

    # Load existing queue
    queue = _load_queue()

    # Check for duplicate -- same URL already queued and not yet processed
    for item in queue:
        if item["url"] == canonical_url and item["status"] in ("queued", "processing"):
            return {
                "status": "already_queued",
                "queue_id": item["queue_id"],
                "url": canonical_url,
                "queued_at": item["queued_at"],
                "message": f"URL already in queue as {item['queue_id']} (status: {item['status']})"
            }

    # Build queue entry
    entry = {
        "queue_id": queue_id,
        "url": canonical_url,
        "original_url": original_url,
        "priority": priority,
        "status": "queued",
        "queued_at": now.isoformat(),
        "entry_id": entry_id,
        "context": context,
        "tags": tags or [],
        "wait_seconds": wait_seconds,
        "attempts": 0,
        "last_error": None,
        "staged_path": None,
        "completed_at": None,
    }

    queue.append(entry)
    _save_queue(queue)

    # Also queue for SPN preservation (fire-and-forget)
    enqueue_spn(canonical_url)

    # Count position (1-based, among queued items only)
    queued_items = [item for item in queue if item["status"] == "queued"]
    position = len(queued_items)

    return {
        "status": "queued",
        "queue_id": queue_id,
        "url": canonical_url,
        "original_url": original_url,
        "priority": priority,
        "position": position,
        "total_queued": len(queued_items),
        "message": f"URL queued for async archiving (position {position})"
    }


def _load_queue():
    """Load the queue from disk."""
    if ARCHIVE_QUEUE_FILE.exists():
        try:
            return json.loads(ARCHIVE_QUEUE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return []
    return []


def _save_queue(queue):
    """Save the queue to disk atomically."""
    temp_path = ARCHIVE_QUEUE_FILE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(ARCHIVE_QUEUE_FILE)


if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = queue_archive_impl(**params)
    output(result)
