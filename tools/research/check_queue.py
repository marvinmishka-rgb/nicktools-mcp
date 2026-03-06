"""Check the status of the async archive queue.
---
description: Check archive queue status and read completed captures
creates_nodes: []
creates_edges: []
databases: []
---

Returns the current state of the archive queue: queued items, processing
items, completed captures with paths to staged results, and failed items.

Use read_staged to retrieve the structured extraction for a completed item.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))

import json
from pathlib import Path

from lib.io import setup_output, load_params, output
from lib.paths import ARCHIVE_QUEUE_FILE, ARCHIVE_STAGED_DIR


def check_queue_impl(queue_id=None, status_filter=None, **kwargs):
    """Check archive queue status.

    Args:
        queue_id: Optional -- check a specific queue entry by ID
        status_filter: Optional -- filter by status: "queued", "processing",
                       "completed", "failed", "all" (default: shows summary)

    Returns:
        dict with queue summary and optionally filtered items
    """
    queue = _load_queue()

    if not queue:
        return {
            "total": 0,
            "message": "Archive queue is empty. Use queue_archive to submit URLs.",
            "items": []
        }

    # If specific queue_id requested, return just that entry
    if queue_id:
        for item in queue:
            if item["queue_id"] == queue_id:
                result = dict(item)
                # If completed, check if staged file exists and add preview
                if item["status"] == "completed" and item.get("staged_path"):
                    staged = Path(item["staged_path"])
                    if staged.exists():
                        try:
                            staged_data = json.loads(staged.read_text(encoding="utf-8"))
                            result["staged_preview"] = {
                                "title": staged_data.get("title"),
                                "text_size": staged_data.get("text_size"),
                                "extraction_method": staged_data.get("extraction_method"),
                            }
                        except Exception:
                            pass
                return result
        return {"error": f"Queue ID '{queue_id}' not found"}

    # Build summary
    by_status = {}
    for item in queue:
        s = item["status"]
        by_status.setdefault(s, []).append(item)

    summary = {
        "total": len(queue),
        "queued": len(by_status.get("queued", [])),
        "processing": len(by_status.get("processing", [])),
        "completed": len(by_status.get("completed", [])),
        "failed": len(by_status.get("failed", [])),
    }

    # Also check for staged files that aren't in the queue (manual captures)
    staged_files = list(ARCHIVE_STAGED_DIR.glob("*.json")) if ARCHIVE_STAGED_DIR.exists() else []
    summary["staged_files"] = len(staged_files)

    # Return filtered items if requested
    items = []
    if status_filter and status_filter != "all":
        items = by_status.get(status_filter, [])
    elif status_filter == "all":
        items = queue
    else:
        # Default: show queued and processing items (actionable), plus recent completed
        items = by_status.get("queued", []) + by_status.get("processing", [])
        # Add last 5 completed items
        completed = by_status.get("completed", [])
        if completed:
            items += completed[-5:]

    # Compact items for display
    compact_items = []
    for item in items:
        compact = {
            "queue_id": item["queue_id"],
            "url": item["url"],
            "status": item["status"],
            "queued_at": item["queued_at"],
            "priority": item.get("priority", "normal"),
        }
        if item.get("completed_at"):
            compact["completed_at"] = item["completed_at"]
        if item.get("last_error"):
            compact["last_error"] = item["last_error"]
        if item.get("staged_path"):
            compact["staged_path"] = item["staged_path"]
        if item.get("attempts", 0) > 0:
            compact["attempts"] = item["attempts"]
        compact_items.append(compact)

    summary["items"] = compact_items
    return summary


def _load_queue():
    """Load the queue from disk."""
    if ARCHIVE_QUEUE_FILE.exists():
        try:
            return json.loads(ARCHIVE_QUEUE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return []
    return []


if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = check_queue_impl(**params)
    output(result)
