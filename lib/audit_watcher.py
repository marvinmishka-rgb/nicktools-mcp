"""
Layer 1 -- Live audit watcher for Cowork sessions.

Background thread that monitors the current session's audit.jsonl in real-time,
extracts structured signals from tool calls, and writes session metadata + context
to Neo4j incrementally.

Imports from: lib.paths (L0), lib.db (L0), lib.audit_parser (L0),
              lib.call_monitor (L0), lib.task_tracker (L1)

Design:
  - Single daemon thread, started once at server startup
  - Polls audit.jsonl for growth every POLL_INTERVAL seconds
  - Reads new lines from last-known offset (append-only file)
  - Extracts 3 categories of signals:
      1. Session metadata (stats, tool counts) -> CoworkSession node
      2. Tool result signals (entity names, source URLs) -> CoworkSession arrays
      3. Error intelligence (repeated failures) -> CoworkSession + call_monitor
  - Writes to Neo4j through execute_write (metadata) -- NOT raw Cypher MCP
  - Does NOT create graph entities speculatively (conservative Phase 2 design)

Thread safety: all shared state behind _lock. Neo4j writes use shared driver.
"""

import json
import os
import sys
import threading
import time
import traceback
from collections import Counter
from datetime import datetime, timezone

from lib.db import execute_write, ENTRY_DATABASE
from lib.audit_parser import parse_audit_streaming
from lib.task_tracker import register_task, update_task

# -- Configuration --
POLL_INTERVAL = 10       # seconds between polls
IDLE_TIMEOUT = 300       # seconds of no growth before marking session "idle"
MAX_ARRAY_SIZE = 200     # max items in mentionedEntities/capturedSources arrays
WRITE_BATCH_SIZE = 5     # min new lines before writing to Neo4j (reduces write frequency)

# -- Module state --
_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event = threading.Event()
_state = {
    "status": "stopped",        # stopped | starting | running | error | idle
    "audit_path": None,
    "session_id": None,
    "process_name": None,
    "last_offset": 0,           # byte offset in file
    "last_line": 0,             # line count processed
    "last_poll": None,          # ISO timestamp
    "last_growth": None,        # ISO timestamp of last file growth
    "polls": 0,
    "lines_processed": 0,
    "neo4j_writes": 0,
    "errors": [],               # last 10 errors
    "task_id": None,
}

# -- Accumulated signals (written to Neo4j periodically) --
_signals = {
    "tool_calls": Counter(),        # tool_name -> count
    "user_messages": 0,
    "assistant_messages": 0,
    "total_lines": 0,
    "mentioned_entities": [],       # entity names seen in graph tool results
    "captured_sources": [],         # URLs from archive/capture results
    "produced_entries": [],         # entry IDs from create_entry results
    "error_signals": [],            # structured error data
    "last_timestamp": None,         # latest _audit_timestamp seen
    "pending_lines": 0,            # lines since last Neo4j write
}


# ============================================================
# Signal extractors
# ============================================================

def _extract_signals(entry):
    """Extract structured signals from a single audit entry.

    Modifies _signals in place (caller holds lock or is the watcher thread).
    """
    t = entry.get("type")
    ts = entry.get("_audit_timestamp")
    if ts:
        _signals["last_timestamp"] = ts

    _signals["total_lines"] += 1

    if t == "user":
        _signals["user_messages"] += 1

    elif t == "assistant":
        msg = entry.get("message", {})
        _signals["assistant_messages"] += 1
        for block in msg.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tool_name = block.get("name", "unknown")
                _signals["tool_calls"][tool_name] += 1
                _extract_tool_call_signals(tool_name, block.get("input", {}))

    # Tool results come back as user messages with tool_result content
    if t == "user":
        msg = entry.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    _extract_tool_result_signals(block)


def _extract_tool_call_signals(tool_name, input_dict):
    """Extract signals from tool call inputs (what Claude is trying to do)."""
    if not isinstance(input_dict, dict):
        return

    # graph operations: extract entity names from params
    if tool_name == "mcp__nicktools__graph":
        op = input_dict.get("operation", "")
        params_str = input_dict.get("params", "")
        if op in ("node", "write", "commit", "add_person", "add_organization",
                   "add_event", "add_document", "add_property"):
            _extract_entity_from_params(params_str)

    # research operations: extract URLs
    elif tool_name == "mcp__nicktools__research":
        op = input_dict.get("operation", "")
        params_str = input_dict.get("params", "")
        if op in ("queue_archive", "archive_source", "save_page"):
            _extract_url_from_params(params_str)

    # entry operations: track creation intent
    elif tool_name == "mcp__nicktools__entry":
        op = input_dict.get("operation", "")
        if op == "create_entry":
            params_str = input_dict.get("params", "")
            _extract_entry_from_params(params_str)


def _extract_entity_from_params(params_str):
    """Pull entity name from graph tool params string."""
    if not params_str:
        return
    try:
        params = json.loads(params_str) if isinstance(params_str, str) else params_str
        name = params.get("name")
        if name and name not in _signals["mentioned_entities"]:
            if len(_signals["mentioned_entities"]) < MAX_ARRAY_SIZE:
                _signals["mentioned_entities"].append(name)

        # For write operations with entities array
        entities = params.get("entities", [])
        for ent in entities:
            if isinstance(ent, dict):
                ename = ent.get("name")
                if ename and ename not in _signals["mentioned_entities"]:
                    if len(_signals["mentioned_entities"]) < MAX_ARRAY_SIZE:
                        _signals["mentioned_entities"].append(ename)

        # For commit operations
        operations = params.get("operations", [])
        for op in operations:
            if isinstance(op, dict):
                oname = op.get("name")
                if oname and oname not in _signals["mentioned_entities"]:
                    if len(_signals["mentioned_entities"]) < MAX_ARRAY_SIZE:
                        _signals["mentioned_entities"].append(oname)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass


def _extract_url_from_params(params_str):
    """Pull URL from research tool params string."""
    if not params_str:
        return
    try:
        params = json.loads(params_str) if isinstance(params_str, str) else params_str
        url = params.get("url") or params.get("path")
        if url and url not in _signals["captured_sources"]:
            if len(_signals["captured_sources"]) < MAX_ARRAY_SIZE:
                _signals["captured_sources"].append(url)
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass


def _extract_entry_from_params(params_str):
    """Pull entry title from create_entry params."""
    if not params_str:
        return
    try:
        params = json.loads(params_str) if isinstance(params_str, str) else params_str
        title = params.get("title")
        if title:
            # Entry ID comes from the result, not the call. Store title as intent.
            pass  # Will be captured from result
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass


def _extract_tool_result_signals(block):
    """Extract signals from tool result content."""
    content = block.get("content", "")
    if not isinstance(content, str):
        return

    # Try to parse as JSON result
    try:
        # Results often have {"result": "{...}"} double-encoding
        outer = json.loads(content) if content.startswith("{") else None
        if outer and isinstance(outer, dict):
            result_str = outer.get("result", "")
            if isinstance(result_str, str) and result_str.startswith("{"):
                result = json.loads(result_str)
            else:
                result = outer
        else:
            result = None
    except (json.JSONDecodeError, TypeError):
        result = None

    if not result or not isinstance(result, dict):
        return

    # Extract entry IDs from create_entry results
    entry_id = result.get("entry_id")
    if entry_id and entry_id not in _signals["produced_entries"]:
        if len(_signals["produced_entries"]) < MAX_ARRAY_SIZE:
            _signals["produced_entries"].append(entry_id)

    # Extract error signals
    error = result.get("error")
    if error:
        error_signal = {
            "error": str(error)[:200],
            "timestamp": _signals["last_timestamp"],
        }
        if len(_signals["error_signals"]) < MAX_ARRAY_SIZE:
            _signals["error_signals"].append(error_signal)

    # Extract source URLs from process_queue results
    results_list = result.get("results", [])
    if isinstance(results_list, list):
        for item in results_list:
            if isinstance(item, dict):
                url = item.get("url")
                if url and url not in _signals["captured_sources"]:
                    if len(_signals["captured_sources"]) < MAX_ARRAY_SIZE:
                        _signals["captured_sources"].append(url)


# ============================================================
# Neo4j writer
# ============================================================

def _write_to_neo4j(driver, session_id, force=False):
    """Write accumulated signals to CoworkSession node.

    Only writes if pending_lines >= WRITE_BATCH_SIZE or force=True.
    Uses C-level Cypher (execute_write) -- appropriate for metadata SET
    operations on an existing node.
    """
    if not force and _signals["pending_lines"] < WRITE_BATCH_SIZE:
        return False

    if not session_id:
        return False

    try:
        # Build top tools list
        top_tools = [t for t, _ in _signals["tool_calls"].most_common(10)]

        # Truncate arrays for Neo4j property storage
        entities = _signals["mentioned_entities"][:MAX_ARRAY_SIZE]
        sources = _signals["captured_sources"][:MAX_ARRAY_SIZE]
        entries = _signals["produced_entries"][:MAX_ARRAY_SIZE]
        errors = [json.dumps(e) for e in _signals["error_signals"][-50:]]

        cypher = """
            MERGE (cs:CoworkSession {sessionId: $sessionId})
            ON CREATE SET cs.createdAt = datetime()
            SET cs.processName = COALESCE(cs.processName, $processName),
                cs.auditPath = COALESCE(cs.auditPath, $auditPath),
                cs.toolCallCount = $toolCallCount,
                cs.userMessageCount = $userMessageCount,
                cs.entryCount = $totalLines,
                cs.topTools = $topTools,
                cs.auditSizeKB = $auditSizeKB,
                cs.liveStatus = $liveStatus,
                cs.watcherLastUpdate = datetime(),
                cs.mentionedEntities = $entities,
                cs.capturedSources = $sources,
                cs.producedEntries = $entries,
                cs.errorSignals = $errors
            RETURN cs.sessionId AS sid
        """

        # Add timestamp if available
        if _signals["last_timestamp"]:
            cypher = cypher.replace(
                "RETURN cs.sessionId AS sid",
                "SET cs.lastAuditTimestamp = datetime($lastTS)\nRETURN cs.sessionId AS sid"
            )

        params = {
            "sessionId": session_id,
            "processName": _state.get("process_name", ""),
            "auditPath": _state.get("audit_path", ""),
            "toolCallCount": sum(_signals["tool_calls"].values()),
            "userMessageCount": _signals["user_messages"],
            "totalLines": _signals["total_lines"],
            "topTools": top_tools,
            "auditSizeKB": round(os.path.getsize(_state["audit_path"]) / 1024) if _state["audit_path"] else 0,
            "liveStatus": _state["status"],
            "entities": entities,
            "sources": sources,
            "entries": entries,
            "errors": errors,
        }
        if _signals["last_timestamp"]:
            params["lastTS"] = _signals["last_timestamp"]

        execute_write(cypher, database=ENTRY_DATABASE, driver=driver, **params)

        _signals["pending_lines"] = 0
        with _lock:
            _state["neo4j_writes"] += 1
        return True

    except Exception as e:
        _record_error(f"Neo4j write failed: {e}")
        return False


# ============================================================
# Poll loop
# ============================================================

def _poll_loop(audit_path, session_id, driver):
    """Main polling loop. Runs in daemon thread."""
    with _lock:
        _state["status"] = "running"
        _state["last_growth"] = _now()

    # Register as visible background task
    task_id = register_task(
        operation="audit_watcher",
        description=f"Live audit watcher for {_state.get('process_name', '?')}",
    )
    with _lock:
        _state["task_id"] = task_id

    last_size = 0
    file_offset = 0
    line_count = 0

    # Start from current end of file (don't replay history)
    try:
        last_size = os.path.getsize(audit_path)
        file_offset = last_size
        # Count existing lines to set baseline
        with open(audit_path, encoding="utf-8", errors="replace") as f:
            line_count = sum(1 for _ in f)
        with _lock:
            _state["last_offset"] = file_offset
            _state["last_line"] = line_count
        _signals["total_lines"] = line_count
    except Exception as e:
        _record_error(f"Initial file read failed: {e}")

    print(f"[watcher] Started: {audit_path} (offset={file_offset}, lines={line_count})",
          file=sys.stderr)

    while not _stop_event.is_set():
        _stop_event.wait(POLL_INTERVAL)
        if _stop_event.is_set():
            break

        try:
            current_size = os.path.getsize(audit_path)
        except FileNotFoundError:
            _record_error("Audit file disappeared")
            continue

        with _lock:
            _state["polls"] += 1
            _state["last_poll"] = _now()

        if current_size <= last_size:
            # No growth -- check idle timeout
            if _state.get("last_growth"):
                try:
                    growth_ts = datetime.fromisoformat(_state["last_growth"])
                    idle_secs = (datetime.now(timezone.utc) - growth_ts).total_seconds()
                    if idle_secs > IDLE_TIMEOUT and _state["status"] == "running":
                        with _lock:
                            _state["status"] = "idle"
                        _write_to_neo4j(driver, session_id, force=True)
                except Exception:
                    pass
            continue

        # File grew -- read new data
        with _lock:
            _state["last_growth"] = _now()
            if _state["status"] == "idle":
                _state["status"] = "running"

        new_lines = []
        try:
            with open(audit_path, encoding="utf-8", errors="replace") as f:
                f.seek(file_offset)
                raw = f.read()
                file_offset = f.tell()

            for line_str in raw.splitlines():
                line_str = line_str.strip()
                if not line_str:
                    continue
                try:
                    entry = json.loads(line_str)
                    new_lines.append(entry)
                except (json.JSONDecodeError, ValueError):
                    continue
        except Exception as e:
            _record_error(f"File read error: {e}")
            continue

        last_size = current_size

        # Process new lines
        for entry in new_lines:
            _extract_signals(entry)
            line_count += 1
            _signals["pending_lines"] += 1

        with _lock:
            _state["last_offset"] = file_offset
            _state["last_line"] = line_count
            _state["lines_processed"] += len(new_lines)

        # Write to Neo4j if enough data accumulated
        _write_to_neo4j(driver, session_id)

        # Update task tracker
        update_task(
            task_id,
            items_completed=_state["lines_processed"],
            result_summary=f"lines={line_count}, entities={len(_signals['mentioned_entities'])}, "
                          f"sources={len(_signals['captured_sources'])}"
        )

    # Final flush on shutdown
    _write_to_neo4j(driver, session_id, force=True)
    update_task(task_id, status="completed",
                result_summary=f"Watcher stopped. {_state['lines_processed']} lines, "
                              f"{_state['neo4j_writes']} writes")
    with _lock:
        _state["status"] = "stopped"

    print(f"[watcher] Stopped: {_state['lines_processed']} lines, "
          f"{_state['neo4j_writes']} Neo4j writes", file=sys.stderr)


# ============================================================
# Public API
# ============================================================

def start_watcher(audit_path, session_id=None, process_name=None, driver=None):
    """Start the live audit watcher.

    Args:
        audit_path: Path to audit.jsonl file to watch
        session_id: CoworkSession sessionId (e.g., 'local_abc123...')
        process_name: VM process name (e.g., 'clever-gracious-tesla')
        driver: Shared Neo4j driver
    """
    global _thread

    with _lock:
        if _thread and _thread.is_alive():
            print("[watcher] Already running, skipping start", file=sys.stderr)
            return

    if not audit_path or not os.path.exists(audit_path):
        print(f"[watcher] Audit file not found: {audit_path}", file=sys.stderr)
        return

    if not driver:
        print("[watcher] No Neo4j driver provided, cannot start", file=sys.stderr)
        return

    with _lock:
        _state["status"] = "starting"
        _state["audit_path"] = audit_path
        _state["session_id"] = session_id
        _state["process_name"] = process_name
        _state["last_offset"] = 0
        _state["last_line"] = 0
        _state["lines_processed"] = 0
        _state["neo4j_writes"] = 0
        _state["polls"] = 0
        _state["errors"] = []

    # Reset signals
    _signals["tool_calls"] = Counter()
    _signals["user_messages"] = 0
    _signals["assistant_messages"] = 0
    _signals["total_lines"] = 0
    _signals["mentioned_entities"] = []
    _signals["captured_sources"] = []
    _signals["produced_entries"] = []
    _signals["error_signals"] = []
    _signals["last_timestamp"] = None
    _signals["pending_lines"] = 0

    _stop_event.clear()
    _thread = threading.Thread(
        target=_poll_loop,
        args=(audit_path, session_id, driver),
        daemon=True,
        name="audit-watcher"
    )
    _thread.start()


def stop_watcher():
    """Stop the watcher gracefully."""
    global _thread
    if _thread and _thread.is_alive():
        _stop_event.set()
        _thread.join(timeout=15)
        _thread = None


def get_watcher_status():
    """Return current watcher state for the management interface.

    Returns:
        dict with status, counters, signal summaries
    """
    with _lock:
        status = dict(_state)
    status["signals"] = {
        "tool_calls": dict(_signals["tool_calls"].most_common(10)),
        "user_messages": _signals["user_messages"],
        "assistant_messages": _signals["assistant_messages"],
        "total_lines": _signals["total_lines"],
        "mentioned_entities_count": len(_signals["mentioned_entities"]),
        "mentioned_entities": _signals["mentioned_entities"][:20],
        "captured_sources_count": len(_signals["captured_sources"]),
        "captured_sources": _signals["captured_sources"][:10],
        "produced_entries": _signals["produced_entries"],
        "error_count": len(_signals["error_signals"]),
        "pending_lines": _signals["pending_lines"],
    }
    return status


# -- Helpers --

def _now():
    return datetime.now(timezone.utc).isoformat()


def _record_error(msg):
    """Log an error to state (keeps last 10) and stderr."""
    print(f"[watcher] ERROR: {msg}", file=sys.stderr)
    with _lock:
        _state["errors"].append({"time": _now(), "msg": str(msg)[:300]})
        _state["errors"] = _state["errors"][-10:]
