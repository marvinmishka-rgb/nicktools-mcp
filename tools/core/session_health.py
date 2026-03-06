"""Session health and cross-context-window intelligence.
---
description: Get actionable session health summary -- error patterns, known failures, guidance
databases: []
---

Designed to be called at the start of a new context window (after a context reset)
or proactively to check system state. Returns everything a new context window needs
to avoid repeating failures from earlier in the session.

Usage:
    core("session_health")                    # Full health report
    core("session_health", '{"brief": true}') # Just guidance lines
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def session_health_impl(brief: bool = False, driver=None, **kwargs) -> str:
    """Return session health summary with cross-context-window intelligence.

    Args:
        brief: If true, return only guidance lines and error rate (minimal tokens)
        driver: Neo4j driver (unused, passed by dispatcher)

    Returns:
        JSON with session summary, error patterns, known failures, and guidance.
    """
    from lib.call_monitor import get_session_summary, get_error_patterns, get_stats

    summary = get_session_summary()

    if brief:
        # Minimal output for token efficiency -- just what you need to know
        result = {
            "uptime_minutes": summary["uptime_minutes"],
            "total_calls": summary["total_calls"],
            "error_rate": summary["error_rate"],
            "guidance": summary["guidance"],
            "healthy": summary["healthy"],
        }
        if summary["guidance"]:
            result["summary"] = f"[!] {len(summary['guidance'])} known issue(s) -- read guidance before proceeding"
        else:
            result["summary"] = "[OK] No known issues -- system healthy"
        return json.dumps(result, indent=2)

    # Full report
    error_patterns = get_error_patterns()
    rolling_stats = get_stats()

    result = {
        "session": {
            "uptime_minutes": summary["uptime_minutes"],
            "total_calls": summary["total_calls"],
            "total_errors": summary["total_errors"],
            "error_rate": summary["error_rate"],
        },
        "rolling_window": {
            "window_calls": rolling_stats["total_calls"],
            "calls_per_minute": rolling_stats["calls_per_minute"],
            "top_operations": rolling_stats["top_operations"],
        },
        "known_failures": summary["known_failures"],
        "error_patterns": error_patterns,
        "guidance": summary["guidance"],
        "healthy": summary["healthy"],
    }

    if summary["guidance"]:
        result["summary"] = f"[!] {len(summary['guidance'])} known issue(s) -- review guidance"
    else:
        result["summary"] = "[OK] System healthy -- no error patterns detected"

    return json.dumps(result, indent=2)


if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    result = session_health_impl(**params)
    output(result)
