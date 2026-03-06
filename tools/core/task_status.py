"""Query background task status and history.
---
description: View active/recent background tasks (archive queue, SPN, captures)
databases: []
---
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.task_tracker import get_tasks, get_task, get_active_count


def task_status_impl(task_id=None, operation=None, status=None,
                     limit=20, driver=None, **kwargs):
    """Query background task status.

    Args:
        task_id: Get a single task by ID
        operation: Filter by operation (e.g., 'process_queue', 'spn_drain')
        status: Filter by status ('active', 'completed', 'failed', 'partial')
        limit: Max tasks to return (default 20)

    Returns:
        dict with active_count, tasks list, and summary
    """
    # Single task lookup
    if task_id:
        task = get_task(task_id)
        if task:
            return {
                "active_count": get_active_count(),
                "tasks": [task],
                "summary": f"Task {task_id}: {task['status']}",
            }
        return {
            "active_count": get_active_count(),
            "tasks": [],
            "summary": f"Task {task_id} not found",
        }

    # Filtered query
    tasks = get_tasks(operation=operation, status=status, limit=limit)
    active = get_active_count()

    # Build summary
    if not tasks:
        summary = "No tasks found"
        if operation:
            summary += f" for operation '{operation}'"
        if status:
            summary += f" with status '{status}'"
    else:
        by_status = {}
        for t in tasks:
            s = t.get("status", "unknown")
            by_status[s] = by_status.get(s, 0) + 1
        parts = [f"{count} {st}" for st, count in sorted(by_status.items())]
        summary = f"{len(tasks)} tasks ({', '.join(parts)})"
        if active > 0:
            summary += f" | {active} currently active"

    result = {
        "active_count": active,
        "task_count": len(tasks),
        "tasks": tasks,
        "summary": summary,
    }

    # Include dispatch health summary when no filters are applied (system pulse view)
    if not operation and not status:
        try:
            from lib.call_monitor import get_stats, check_repetition, check_error_cluster
            stats = get_stats()
            warnings = []
            # Check for any repetition warnings across recent unique operations
            for op in set(c.get("op") for c in stats.get("recent", []) if c.get("op")):
                rep = check_repetition(op)
                if rep:
                    warnings.append(rep["message"])
            errors = check_error_cluster()
            if errors:
                for e in errors:
                    warnings.append(f"{e['operation']}: {e['consecutive_errors']} consecutive errors")

            result["dispatch"] = {
                "total_calls": stats.get("total_calls", 0),
                "unique_operations": stats.get("unique_operations", 0),
                "warnings": warnings if warnings else None,
            }
        except Exception:
            pass

    return result


if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    result = task_status_impl(**params)
    output(result)
