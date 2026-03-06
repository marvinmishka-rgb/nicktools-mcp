"""
Layer 1 -- Background task tracking.

In-memory registry of active/recent tasks with JSONL persistence.
Gives Claude visibility into background operations (SPN queue, process_queue batches,
long-running captures) without requiring tool-specific queries.

Integration points:
  - lib/spn.py: enqueue_spn() registers task, drain_spn_queue() updates status
  - tools/research/process_queue.py: batch registered on start, updated per-item
  - tools/core/task_status.py: query interface for Claude

Imports from: lib.paths (Layer 0)
"""
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from lib.paths import CLAUDE_FILES

# -- Constants --
TASK_LOG_FILE = CLAUDE_FILES / "task_tracker.jsonl"
MAX_MEMORY_TASKS = 200
PRUNE_AGE_HOURS = 24


# -- Module state --
_lock = threading.Lock()
_tasks: dict = {}  # task_id -> task_dict
_initialized = False


def _ensure_loaded():
    """Load task history from JSONL on first access, then compact."""
    global _initialized
    if _initialized:
        return
    with _lock:
        if _initialized:
            return
        if TASK_LOG_FILE.exists():
            try:
                for line in TASK_LOG_FILE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        tid = record.get("task_id")
                        if tid:
                            _tasks[tid] = record
                    except json.JSONDecodeError:
                        continue
            except Exception:
                pass

            # Compact: drop tasks older than PRUNE_AGE_HOURS, rewrite with latest state only
            _compact_jsonl()

        _initialized = True


def _persist(task: dict):
    """Append a task record to JSONL log."""
    try:
        TASK_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(TASK_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")
    except Exception:
        pass  # Non-critical -- in-memory state is the primary source


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _generate_id(operation: str) -> str:
    """Generate a unique task ID: {operation}-{timestamp_ms}."""
    ts = int(time.time() * 1000) % 10_000_000  # Last 7 digits
    return f"{operation}-{ts}"


# -- Public API --

def register_task(operation: str, params: Optional[dict] = None,
                  description: str = "", batch_size: int = 0) -> str:
    """Register a new background task.

    Args:
        operation: Tool operation name (e.g., 'process_queue', 'spn_drain')
        params: Task parameters (for debugging context)
        description: Human-readable description
        batch_size: Expected number of items (0 = unknown)

    Returns:
        task_id string
    """
    _ensure_loaded()
    task_id = _generate_id(operation)

    task = {
        "task_id": task_id,
        "operation": operation,
        "status": "active",
        "description": description or f"{operation} task",
        "batch_size": batch_size,
        "items_completed": 0,
        "items_failed": 0,
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "result_summary": None,
        "errors": [],
        "params_preview": _safe_preview(params),
    }

    with _lock:
        _tasks[task_id] = task
        _prune_if_needed()

    _persist(task)
    return task_id


def update_task(task_id: str, status: Optional[str] = None,
                items_completed: Optional[int] = None,
                items_failed: Optional[int] = None,
                result_summary: Optional[str] = None,
                error: Optional[str] = None):
    """Update an existing task's status.

    Args:
        task_id: Task to update
        status: New status ('active', 'completed', 'failed', 'partial')
        items_completed: Increment completed count
        items_failed: Increment failed count
        result_summary: Brief result description
        error: Error message to append
    """
    _ensure_loaded()
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return

        if status:
            task["status"] = status
        if items_completed is not None:
            task["items_completed"] = items_completed
        if items_failed is not None:
            task["items_failed"] = items_failed
        if result_summary:
            task["result_summary"] = result_summary
        if error:
            task["errors"] = task.get("errors", [])
            task["errors"].append(error[:300])
            # Keep last 10 errors
            task["errors"] = task["errors"][-10:]

        task["updated_at"] = _now_iso()

    _persist(task)


def get_tasks(operation: Optional[str] = None,
              status: Optional[str] = None,
              limit: int = 20) -> list:
    """Query tasks with optional filters.

    Args:
        operation: Filter by operation name
        status: Filter by status ('active', 'completed', 'failed', 'partial')
        limit: Max tasks to return (most recent first)

    Returns:
        List of task dicts
    """
    _ensure_loaded()
    with _lock:
        tasks = list(_tasks.values())

    # Filter
    if operation:
        tasks = [t for t in tasks if t.get("operation") == operation]
    if status:
        tasks = [t for t in tasks if t.get("status") == status]

    # Sort by updated_at descending
    tasks.sort(key=lambda t: t.get("updated_at", ""), reverse=True)

    return tasks[:limit]


def get_task(task_id: str) -> Optional[dict]:
    """Get a single task by ID."""
    _ensure_loaded()
    with _lock:
        return _tasks.get(task_id)


def get_active_count() -> int:
    """Quick count of active tasks."""
    _ensure_loaded()
    with _lock:
        return sum(1 for t in _tasks.values() if t.get("status") == "active")


# -- Helpers --

def _safe_preview(params: Optional[dict]) -> Optional[str]:
    """Create a safe, short preview of task params."""
    if not params:
        return None
    try:
        preview = json.dumps(params, ensure_ascii=False)
        return preview[:200] + "..." if len(preview) > 200 else preview
    except Exception:
        return None


def _prune_if_needed():
    """Remove old completed tasks from memory (already in JSONL for audit)."""
    if len(_tasks) <= MAX_MEMORY_TASKS:
        return

    now = time.time()
    to_remove = []
    for tid, task in _tasks.items():
        if task.get("status") in ("completed", "failed"):
            try:
                updated = datetime.fromisoformat(task["updated_at"])
                age_hours = (now - updated.timestamp()) / 3600
                if age_hours > PRUNE_AGE_HOURS:
                    to_remove.append(tid)
            except Exception:
                continue

    for tid in to_remove:
        del _tasks[tid]


def _compact_jsonl():
    """Rewrite JSONL with only latest state per task, dropping old completed tasks.

    Called once on first load. Reduces file size from unbounded append-only growth
    to at most MAX_MEMORY_TASKS records with latest state only.
    """
    if not _tasks:
        return

    now = time.time()
    COMPACT_AGE_DAYS = 7

    # Keep only tasks younger than 7 days
    keep = {}
    for tid, task in _tasks.items():
        try:
            updated = datetime.fromisoformat(task["updated_at"])
            age_days = (now - updated.timestamp()) / 86400
            if age_days <= COMPACT_AGE_DAYS:
                keep[tid] = task
        except Exception:
            keep[tid] = task  # Keep if we can't parse the date

    # Only rewrite if we actually dropped something
    if len(keep) == len(_tasks):
        return

    # Update in-memory state
    _tasks.clear()
    _tasks.update(keep)

    # Rewrite JSONL atomically
    try:
        temp = TASK_LOG_FILE.with_suffix(".tmp")
        with open(temp, "w", encoding="utf-8") as f:
            for task in keep.values():
                f.write(json.dumps(task, ensure_ascii=False) + "\n")
        temp.replace(TASK_LOG_FILE)
    except Exception:
        pass
