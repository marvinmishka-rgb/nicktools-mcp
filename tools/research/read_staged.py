"""Read a staged archive result and optionally mark it as processed.
---
description: Read structured extraction from a completed async capture
creates_nodes: []
creates_edges: []
databases: []
---

Reads the structured JSON produced by the background capture worker
for a completed queue item. Returns clean article text, metadata,
and (when available) extracted claims and entity mentions.

Use this after check_queue shows items with status "completed".
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))

import json
from pathlib import Path

from lib.io import setup_output, load_params, output
from lib.paths import ARCHIVE_QUEUE_FILE, ARCHIVE_STAGED_DIR


def read_staged_impl(queue_id=None, url_hash=None, mark_processed=False, **kwargs):
    """Read a staged archive extraction.

    Args:
        queue_id: The queue ID of a completed capture
        url_hash: Alternatively, the URL hash to look up directly in staged/
        mark_processed: If True, update queue status to "processed"

    Returns:
        dict with clean text, metadata, and optionally claims/entities
    """
    staged_path = None

    if queue_id:
        # Look up in queue to find staged path
        queue = _load_queue()
        for item in queue:
            if item["queue_id"] == queue_id:
                if item["status"] not in ("completed", "processed"):
                    return {"error": f"Queue item {queue_id} is not completed (status: {item['status']})"}
                staged_path = item.get("staged_path")
                if not staged_path:
                    return {"error": f"Queue item {queue_id} has no staged_path -- capture may not have completed"}
                break
        else:
            return {"error": f"Queue ID '{queue_id}' not found"}

    elif url_hash:
        # Look directly in staged directory
        staged_path = str(ARCHIVE_STAGED_DIR / f"{url_hash}.json")
    else:
        return {"error": "Provide either 'queue_id' or 'url_hash' to identify the staged capture"}

    # Read the staged file
    staged_file = Path(staged_path)
    if not staged_file.exists():
        return {"error": f"Staged file not found: {staged_path}"}

    try:
        data = json.loads(staged_file.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"Failed to read staged file: {e}"}

    # Mark as processed if requested
    if mark_processed and queue_id:
        queue = _load_queue()
        for item in queue:
            if item["queue_id"] == queue_id:
                item["status"] = "processed"
                break
        _save_queue(queue)
        data["_marked_processed"] = True

    return data


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
    result = read_staged_impl(**params)
    output(result)
