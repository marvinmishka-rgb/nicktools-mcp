"""
Layer 0 -- Cowork session detection from filesystem.

Scans AppData for Cowork session metadata files and identifies the current
session by matching the VM process name. Caches result for server lifetime.

No Neo4j dependency -- this is pure filesystem scanning. The cached result
is used by server.py (startup logging), create_entry.py (PRODUCED_IN linking),
and dispatch_health.py (session context).

Public API (used by tool scripts):
  - find_session_dir()   — locate the account/org directory with session JSONs
  - scan_sessions()      — return basic metadata for all sessions
  - read_session_meta()  — read a single session's metadata JSON
  - detect_current_session() — match by VM process name (cached)
  - get_cached_session()     — return cached result without re-scanning
  - reset_cache()            — clear cache (used by restart_server)
  - SESSIONS_BASE            — constant path to session root

Detection strategy:
  1. Scan AppData/Roaming/Claude/local-agent-mode-sessions/
  2. Read each local_*.json metadata file
  3. Match processName against the target (defaults to newest session)
  4. Return structured metadata + audit stats

No internal dependencies beyond paths.py.
"""
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from lib.paths import USER_HOME

# -- Constants --
SESSIONS_BASE = os.path.join(
    str(USER_HOME), "AppData", "Roaming", "Claude", "local-agent-mode-sessions"
)

# -- Module-scope cache --
_lock = threading.Lock()
_cached_session: dict | None = None
_detection_done: bool = False


def find_session_dir() -> str | None:
    """Find the workspace-specific session directory (account/org level).

    Returns the path to the directory containing local_*.json metadata files,
    or None if no session directory found.
    """
    if not os.path.exists(SESSIONS_BASE):
        return None
    for account in os.listdir(SESSIONS_BASE):
        account_path = os.path.join(SESSIONS_BASE, account)
        if not os.path.isdir(account_path) or account in ("skills-plugin",):
            continue
        for org in os.listdir(account_path):
            org_path = os.path.join(account_path, org)
            if not os.path.isdir(org_path):
                continue
            has_sessions = any(
                f.startswith("local_") and f.endswith(".json")
                for f in os.listdir(org_path)
            )
            if has_sessions:
                return org_path
    return None


def read_session_meta(session_dir: str, meta_filename: str) -> dict | None:
    """Read a single session metadata JSON and return structured record.

    Returns dict with keys: sessionId, title, processName, model,
    createdAt, createdAtEpochMs, lastActivityAt, auditPath, auditSizeKB.
    """
    meta_path = os.path.join(session_dir, meta_filename)
    try:
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:
        return None

    session_id = meta_filename[:-5]  # strip .json
    record = {
        "sessionId": session_id,
        "title": meta.get("title", ""),
        "processName": meta.get("processName", ""),
        "model": meta.get("model", ""),
    }

    # Convert epoch ms timestamps
    if meta.get("createdAt"):
        try:
            record["createdAt"] = datetime.fromtimestamp(
                meta["createdAt"] / 1000, tz=timezone.utc
            ).isoformat()
            record["createdAtEpochMs"] = meta["createdAt"]
        except Exception:
            pass
    if meta.get("lastActivityAt"):
        try:
            record["lastActivityAt"] = datetime.fromtimestamp(
                meta["lastActivityAt"] / 1000, tz=timezone.utc
            ).isoformat()
        except Exception:
            pass

    # Check for audit file
    audit_path = os.path.join(session_dir, session_id, "audit.jsonl")
    if os.path.exists(audit_path):
        record["auditPath"] = audit_path
        record["auditSizeKB"] = round(os.path.getsize(audit_path) / 1024)

    return record


def scan_sessions(session_dir: str | None = None) -> list[dict]:
    """Scan all session metadata files and return basic records.

    Args:
        session_dir: Path to session directory. If None, calls find_session_dir().

    Returns:
        List of session metadata dicts (same structure as read_session_meta),
        sorted by filename. Returns empty list if no sessions found.
    """
    if session_dir is None:
        session_dir = find_session_dir()
    if not session_dir:
        return []
    sessions = []
    for item in sorted(os.listdir(session_dir)):
        if not item.startswith("local_") or not item.endswith(".json"):
            continue
        record = read_session_meta(session_dir, item)
        if record:
            sessions.append(record)
    return sessions


def detect_current_session(process_name: str | None = None) -> dict | None:
    """Detect the current Cowork session from filesystem metadata.

    Args:
        process_name: VM process name to match (e.g., 'compassionate-ecstatic-newton').
                      If None, returns the most recently created session.

    Returns:
        Session metadata dict, or None if no session found.
        Keys: sessionId, title, processName, model, createdAt, lastActivityAt,
              auditPath, auditSizeKB
    """
    global _cached_session, _detection_done

    with _lock:
        if _detection_done:
            return _cached_session

    session_dir = find_session_dir()
    if not session_dir:
        with _lock:
            _detection_done = True
        return None

    # Scan all session metadata files
    sessions = scan_sessions(session_dir)

    if not sessions:
        with _lock:
            _detection_done = True
        return None

    # Match by process name if provided
    if process_name:
        for s in sessions:
            if s.get("processName") == process_name:
                with _lock:
                    _cached_session = s
                    _detection_done = True
                return s

    # Fallback: return the most recently created session
    sessions.sort(key=lambda x: x.get("createdAtEpochMs", 0), reverse=True)
    result = sessions[0]

    with _lock:
        _cached_session = result
        _detection_done = True
    return result


def get_cached_session() -> dict | None:
    """Return the cached session result without re-scanning.

    Returns None if detect_current_session() hasn't been called yet.
    """
    with _lock:
        return _cached_session


def reset_cache():
    """Clear the cache (used by restart_server to force re-detection)."""
    global _cached_session, _detection_done
    with _lock:
        _cached_session = None
        _detection_done = False
