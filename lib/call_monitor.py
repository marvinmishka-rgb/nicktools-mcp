"""
Layer 0 -- Dispatch call monitor with loop detection, error clusters, and
cumulative error pattern tracking.

Thread-safe rolling window of recent tool calls. Detects:
- Repetitive calls (same operation N+ times in M seconds)
- Error clusters (consecutive failures on same operation)
- Cumulative error patterns (tracks ALL errors across server lifetime for
  cross-context-window learning -- survives context resets within one session)
- Provides recent call history for introspection

No persistence to disk -- window lives for server lifetime, resets on restart.
No internal dependencies beyond stdlib.

Called from server.py _dispatch() to record calls, and from
tools/core/dispatch_health.py and session_health.py to surface diagnostics.
"""
import hashlib
import json
import threading
import time
from collections import deque
from typing import Optional

# -- Configuration --
WINDOW_SIZE = 500           # Max items in rolling window
DEFAULT_REPETITION_WINDOW = 300   # 5 minutes
DEFAULT_REPETITION_THRESHOLD = 4  # 4+ calls = warning
DEFAULT_ERROR_WINDOW = 120        # 2 minutes
DEFAULT_ERROR_THRESHOLD = 2       # 2+ consecutive errors = cluster

# -- Thread-safe rolling window --
_lock = threading.Lock()
_calls: deque = deque(maxlen=WINDOW_SIZE)

# -- Cumulative error patterns (persists across full server lifetime) --
# Unlike the rolling window, this never evicts. It tracks every error seen
# so that new context windows can learn what already failed.
_error_patterns: dict = {}  # {operation: {error_key: {"count": N, "first_ts": T, "last_ts": T}}}
_session_start_ts: float = time.time()
_total_calls: int = 0
_total_errors: int = 0


def _params_hash(params: dict) -> str:
    """Stable hash of params dict for deduplication detection."""
    try:
        raw = json.dumps(params, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:8]
    except Exception:
        return "unhashable"


def record_call(group: str, operation: str, params: dict,
                status: str, duration_ms: float,
                error_key: Optional[str] = None):
    """Record a completed tool call in the rolling window and cumulative patterns.

    Args:
        group: Tool group (graph, research, entry, core)
        operation: Operation name (add_person, archive_source, etc.)
        params: The params dict passed to the tool
        status: "ok" or "error"
        duration_ms: Execution time in milliseconds
        error_key: Short error identifier (first 100 chars of error message)
    """
    global _total_calls, _total_errors
    now = time.time()
    entry = {
        "ts": now,
        "group": group,
        "op": operation,
        "params_hash": _params_hash(params),
        "status": status,
        "duration_ms": round(duration_ms, 1),
        "error_key": error_key,
    }
    with _lock:
        _calls.append(entry)
        _total_calls += 1

        # Track cumulative error patterns
        if status == "error" and error_key:
            _total_errors += 1
            op_errors = _error_patterns.setdefault(operation, {})
            # Normalize error key: strip numeric details, keep pattern
            normalized = _normalize_error(error_key)
            if normalized in op_errors:
                op_errors[normalized]["count"] += 1
                op_errors[normalized]["last_ts"] = now
            else:
                op_errors[normalized] = {
                    "count": 1,
                    "first_ts": now,
                    "last_ts": now,
                    "raw_sample": error_key[:150],
                }


def _normalize_error(error_key: str) -> str:
    """Normalize error keys to group similar errors together.

    Strips variable parts (timestamps, IDs, paths) to find the pattern.
    """
    import re
    # Strip specific file paths (keep just the filename)
    s = re.sub(r'[A-Z]:\\[^\s:]+\\([^\\:]+)', r'\1', error_key)
    # Strip hex IDs and UUIDs
    s = re.sub(r'[0-9a-f]{8,}', '<id>', s)
    # Strip numeric values
    s = re.sub(r'\b\d{4,}\b', '<N>', s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:120]


def check_repetition(operation: str,
                     window_secs: float = DEFAULT_REPETITION_WINDOW,
                     threshold: int = DEFAULT_REPETITION_THRESHOLD) -> Optional[dict]:
    """Check if an operation has been called too frequently.

    Returns warning dict if threshold exceeded, None otherwise.
    """
    now = time.time()
    cutoff = now - window_secs
    with _lock:
        recent = [c for c in _calls if c["op"] == operation and c["ts"] >= cutoff]

    if len(recent) >= threshold:
        # Check if params vary (retries with same args vs. legitimate different calls)
        unique_hashes = {c["params_hash"] for c in recent}
        error_count = sum(1 for c in recent if c["status"] == "error")
        return {
            "warning": "repetitive_calls",
            "operation": operation,
            "count": len(recent),
            "window_secs": window_secs,
            "threshold": threshold,
            "unique_param_sets": len(unique_hashes),
            "error_count": error_count,
            "pattern": "retry_loop" if len(unique_hashes) == 1 else "varied_calls",
        }
    return None


def check_error_cluster(window_secs: float = DEFAULT_ERROR_WINDOW,
                        threshold: int = DEFAULT_ERROR_THRESHOLD) -> list:
    """Check for operations with consecutive errors.

    Returns list of error cluster dicts (one per affected operation).
    """
    now = time.time()
    cutoff = now - window_secs
    with _lock:
        recent = [c for c in _calls if c["ts"] >= cutoff]

    # Group by operation, check for consecutive errors at the tail
    ops: dict[str, list] = {}
    for c in recent:
        ops.setdefault(c["op"], []).append(c)

    clusters = []
    for op, calls in ops.items():
        # Count consecutive errors from the end
        consecutive_errors = 0
        for c in reversed(calls):
            if c["status"] == "error":
                consecutive_errors += 1
            else:
                break

        if consecutive_errors >= threshold:
            last_error = calls[-1].get("error_key", "unknown")
            clusters.append({
                "operation": op,
                "consecutive_errors": consecutive_errors,
                "last_error": last_error,
                "window_secs": window_secs,
            })

    return clusters


def get_recent(limit: int = 10) -> list:
    """Get the most recent N calls from the window."""
    with _lock:
        items = list(_calls)
    return items[-limit:]


def get_stats() -> dict:
    """Get aggregate stats about the call window."""
    now = time.time()
    with _lock:
        items = list(_calls)

    if not items:
        return {
            "total_calls": 0,
            "window_span_secs": 0,
            "calls_per_minute": 0,
            "error_rate": 0,
            "top_operations": [],
        }

    oldest = items[0]["ts"]
    span = now - oldest
    error_count = sum(1 for c in items if c["status"] == "error")

    # Count by operation
    op_counts: dict[str, int] = {}
    for c in items:
        op_counts[c["op"]] = op_counts.get(c["op"], 0) + 1
    top_ops = sorted(op_counts.items(), key=lambda x: -x[1])[:5]

    return {
        "total_calls": len(items),
        "window_span_secs": round(span, 1),
        "calls_per_minute": round(len(items) / max(span / 60, 0.1), 1),
        "error_rate": round(error_count / len(items), 3) if items else 0,
        "error_count": error_count,
        "top_operations": [{"op": op, "count": count} for op, count in top_ops],
    }


def get_dispatch_warning(operation: str) -> Optional[str]:
    """Quick check returning a short warning string, or None.

    Designed to be called before dispatch -- cheap, single-line output.
    """
    rep = check_repetition(operation)
    if rep:
        pattern = rep["pattern"]
        count = rep["count"]
        errors = rep["error_count"]
        if pattern == "retry_loop" and errors > 0:
            return f"[!] {operation} called {count}x (same params, {errors} errors) -- possible retry loop"
        elif pattern == "retry_loop":
            return f"[!] {operation} called {count}x with identical params"
        else:
            return f"[!] {operation} called {count}x in {rep['window_secs']}s"

    clusters = check_error_cluster()
    for cluster in clusters:
        if cluster["operation"] == operation:
            return f"[!] {operation} has {cluster['consecutive_errors']} consecutive errors: {cluster['last_error']}"

    return None


def get_error_patterns() -> dict:
    """Get cumulative error patterns across the entire server lifetime.

    Returns dict with per-operation error breakdowns, sorted by frequency.
    Designed for cross-context-window learning -- call this at session start
    or after a context reset to see what already failed.
    """
    with _lock:
        patterns = {}
        for op, errors in _error_patterns.items():
            op_entries = []
            for pattern, info in sorted(errors.items(), key=lambda x: -x[1]["count"]):
                op_entries.append({
                    "pattern": pattern,
                    "count": info["count"],
                    "sample": info["raw_sample"],
                    "first_seen": info["first_ts"],
                    "last_seen": info["last_ts"],
                })
            if op_entries:
                patterns[op] = op_entries
    return patterns


def get_session_summary() -> dict:
    """Get a compact session health summary for new context windows.

    Returns actionable intelligence: what's working, what's broken,
    what to avoid. Designed to be called once at the start of a new
    context window to bootstrap awareness.
    """
    now = time.time()
    uptime_secs = now - _session_start_ts
    uptime_mins = round(uptime_secs / 60, 1)

    with _lock:
        error_patterns = dict(_error_patterns)
        total_c = _total_calls
        total_e = _total_errors

    # Build "known failures" list -- operations with high error rates
    known_failures = []
    for op, errors in error_patterns.items():
        total_op_errors = sum(e["count"] for e in errors.values())
        top_error = max(errors.values(), key=lambda e: e["count"])
        known_failures.append({
            "operation": op,
            "error_count": total_op_errors,
            "top_pattern": top_error["raw_sample"][:100],
            "last_seen_secs_ago": round(now - top_error["last_ts"], 0),
        })
    known_failures.sort(key=lambda x: -x["error_count"])

    # Build guidance lines based on error patterns
    guidance = []
    for f in known_failures[:5]:
        op = f["operation"]
        sample = f["top_pattern"]
        if "timeout" in sample.lower():
            guidance.append(f"AVOID: {op} is timing out -- check if the target service is responsive")
        elif "403" in sample or "blocked" in sample.lower() or "restricted" in sample.lower():
            guidance.append(f"AVOID: {op} is being blocked (HTTP 403) -- use alternative capture methods")
        elif "connection" in sample.lower():
            guidance.append(f"AVOID: {op} has connection issues -- check network/service status")
        else:
            guidance.append(f"CAUTION: {op} has {f['error_count']} errors -- last: {sample[:80]}")

    return {
        "uptime_minutes": uptime_mins,
        "total_calls": total_c,
        "total_errors": total_e,
        "error_rate": round(total_e / max(total_c, 1), 3),
        "known_failures": known_failures[:10],
        "guidance": guidance,
        "healthy": len(known_failures) == 0,
    }
