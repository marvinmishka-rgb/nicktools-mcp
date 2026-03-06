"""Dispatch health diagnostics -- surfaces call patterns, repetition warnings,
and error clusters from the in-memory call monitor.
---
description: View call patterns, repetition warnings, and error clusters
databases: []
---

Usage:
    core("dispatch_health")                     # Full report
    core("dispatch_health", '{"recent": 20}')   # Last 20 calls
"""
import json


def dispatch_health_impl(recent: int = 10, driver=None) -> str:
    """Return dispatch health diagnostics.

    Args:
        recent: Number of recent calls to include (default 10, max 50)
        driver: Neo4j driver (unused, passed by dispatcher)

    Returns:
        JSON with stats, recent calls, repetition warnings, and error clusters.
    """
    from lib.call_monitor import get_stats, get_recent, check_error_cluster

    recent = min(recent, 50)

    stats = get_stats()
    recent_calls = get_recent(limit=recent)
    error_clusters = check_error_cluster()

    # Format recent calls for readability
    formatted_recent = []
    for c in recent_calls:
        entry = {
            "op": c["op"],
            "status": c["status"],
            "duration_ms": c["duration_ms"],
        }
        if c.get("error_key"):
            entry["error"] = c["error_key"]
        formatted_recent.append(entry)

    # Check for any active repetition warnings across all recent operations
    seen_ops = set()
    repetition_warnings = []
    for c in recent_calls:
        if c["op"] not in seen_ops:
            seen_ops.add(c["op"])
            from lib.call_monitor import check_repetition
            warning = check_repetition(c["op"])
            if warning:
                repetition_warnings.append(warning)

    result = {
        "stats": stats,
        "recent_calls": formatted_recent,
        "repetition_warnings": repetition_warnings,
        "error_clusters": error_clusters,
    }

    # Add a summary line
    issues = len(repetition_warnings) + len(error_clusters)
    if issues == 0:
        result["summary"] = "No dispatch issues detected"
    else:
        parts = []
        if repetition_warnings:
            parts.append(f"{len(repetition_warnings)} repetition warning(s)")
        if error_clusters:
            parts.append(f"{len(error_clusters)} error cluster(s)")
        result["summary"] = f"[!] {', '.join(parts)}"

    return json.dumps(result, indent=2)
