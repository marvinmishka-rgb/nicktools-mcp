"""SPN (Save Page Now) async queue -- Layer 1 module.

Fire-and-forget URL preservation via the Wayback Machine. Completely decoupled
from local capture. Any tool that captures a URL calls enqueue_spn() and moves on.
A separate worker drains the queue respecting rate limits.

Queue file: ClaudeFiles/archive_queue/spn_queue.json
Each item: {url, queued_at, status, attempts, last_error, wayback_url, spn_job_id}

Imports from: lib.paths (Layer 0), lib.archives (Layer 2 -- only submit_to_spn)
Note: This technically imports from Layer 2, but only the standalone SPN API function
which has no dependencies on Layer 1. The import is safe.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from lib.paths import ARCHIVE_QUEUE_DIR
from lib.db import GRAPH_DATABASE, ENTRY_DATABASE

SPN_QUEUE_FILE = ARCHIVE_QUEUE_DIR / "spn_queue.json"

# Rate limit: minimum seconds between SPN submissions
SPN_MIN_DELAY = 8
# Back-off multiplier on 429
SPN_BACKOFF_DELAY = 60
# Max attempts before giving up on a URL
SPN_MAX_ATTEMPTS = 3


def enqueue_spn(url):
    """Add a URL to the SPN queue. Instant, no network calls.

    Deduplicates: if the URL is already queued or completed, skips it.
    Safe to call from any tool -- this is the only integration point.

    Returns:
        dict with {queued: bool, message: str}
    """
    if not url or not url.startswith("http"):
        return {"queued": False, "message": "Invalid URL"}

    queue = _load_queue()

    # Deduplicate by URL
    existing = {item["url"] for item in queue}
    if url in existing:
        return {"queued": False, "message": "Already in SPN queue"}

    queue.append({
        "url": url,
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "status": "queued",
        "attempts": 0,
        "last_error": None,
        "wayback_url": None,
        "spn_job_id": None,
    })
    _save_queue(queue)
    return {"queued": True, "message": f"Queued for SPN: {url[:80]}"}


def drain_spn_queue(batch_size=10, delay=SPN_MIN_DELAY, driver=None):
    """Process queued SPN items. Respects rate limits, backs off on 429.

    Args:
        batch_size: Max items to submit per call
        delay: Seconds between submissions (default 8)
        driver: Neo4j driver for updating Source nodes (optional)

    Returns:
        dict with {processed, submitted, skipped, rate_limited, remaining, task_id}
    """
    # Import here to avoid circular import at module load time
    from lib.archives import submit_to_spn

    queue = _load_queue()
    candidates = [item for item in queue if item["status"] == "queued"]

    if not candidates:
        return {"processed": 0, "submitted": 0, "skipped": 0,
                "rate_limited": False, "remaining": 0,
                "message": "SPN queue empty"}

    # Register task for visibility
    task_id = None
    try:
        from lib.task_tracker import register_task, update_task
        task_id = register_task(
            "spn_drain",
            description=f"SPN queue drain: {len(candidates[:batch_size])} URLs",
            batch_size=len(candidates[:batch_size]),
        )
    except Exception:
        pass

    to_process = candidates[:batch_size]
    submitted = 0
    skipped = 0
    rate_limited = False

    for i, item in enumerate(to_process):
        if i > 0:
            time.sleep(delay)

        url = item["url"]
        result = submit_to_spn(url, if_not_archived_within="30d")
        status = result.get("status", "unknown")

        item["attempts"] = item.get("attempts", 0) + 1

        if status == "submitted":
            item["status"] = "submitted"
            item["spn_job_id"] = result.get("job_id")
            item["wayback_url"] = result.get("wayback_url")
            item["submitted_at"] = datetime.now(timezone.utc).isoformat()
            submitted += 1

            # Update Source node if we have a driver and got a wayback URL
            if driver and result.get("wayback_url"):
                _update_source_wayback(driver, url, result["wayback_url"])

        elif status == "already_archived":
            item["status"] = "completed"
            item["completed_at"] = datetime.now(timezone.utc).isoformat()
            skipped += 1

        elif status == "rate_limited":
            # Don't count this attempt -- just stop the batch
            item["attempts"] -= 1
            rate_limited = True
            _save_queue(queue)
            break

        else:
            item["last_error"] = result.get("detail", "unknown")[:300]
            if item["attempts"] >= SPN_MAX_ATTEMPTS:
                item["status"] = "failed"
            # else stays "queued" for retry

        _save_queue(queue)

    remaining = len([item for item in queue if item["status"] == "queued"])

    # Update task tracker
    if task_id:
        try:
            from lib.task_tracker import update_task
            final_status = "completed" if not rate_limited else "partial"
            update_task(
                task_id,
                status=final_status,
                items_completed=submitted + skipped,
                items_failed=0,
                result_summary=f"submitted={submitted}, skipped={skipped}, remaining={remaining}",
            )
        except Exception:
            pass

    return {
        "processed": len(to_process) if not rate_limited else (submitted + skipped),
        "submitted": submitted,
        "skipped": skipped,
        "rate_limited": rate_limited,
        "remaining": remaining,
        "task_id": task_id,
    }


def spn_queue_status():
    """Get current SPN queue stats without processing anything."""
    queue = _load_queue()
    counts = {"queued": 0, "submitted": 0, "completed": 0, "failed": 0}
    for item in queue:
        s = item.get("status", "queued")
        counts[s] = counts.get(s, 0) + 1
    counts["total"] = len(queue)
    return counts


def _update_source_wayback(driver, url, wayback_url):
    """Update Source node with wayback URL in both databases."""
    from lib.urls import canonicalize_url
    canonical = canonicalize_url(url)
    for db_name in [GRAPH_DATABASE, ENTRY_DATABASE]:
        try:
            with driver.session(database=db_name) as session:
                session.run(
                    "MATCH (s:Source {url: $url}) SET s.waybackUrl = $wb",
                    {"url": canonical, "wb": wayback_url}
                )
        except Exception:
            pass  # Best-effort -- don't crash the queue for a graph update


def _load_queue():
    """Load SPN queue from disk."""
    if SPN_QUEUE_FILE.exists():
        try:
            return json.loads(SPN_QUEUE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, Exception):
            return []
    return []


def _save_queue(queue):
    """Save SPN queue atomically."""
    ARCHIVE_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    temp = SPN_QUEUE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(queue, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(SPN_QUEUE_FILE)
