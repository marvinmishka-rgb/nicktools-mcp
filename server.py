"""
nicktools MCP Server v3.0.0
===========================
Consolidated dispatcher: 5 meta-tools route to 56 operations (12+18+4+22) via TOOL_REGISTRY.
In-process dispatch via importlib.reload (~28ms/call) for most tools,
subprocess for nodriver tools. Shared Neo4j driver created at startup.
Live audit watcher daemon tracks session context for cross-session recovery.

Meta-tools (5):
  graph(operation, params)     -- 10 operations: write, read, node, rel, wire_evidence,
                                  commit, cypher, gds, board_snapshot, deduplicate
  research(operation, params)  -- 18 operations: browse_url, fetch_page, archive_source,
                                  save_page, extract_saved_article, search_pdf, wayback_lookup,
                                  check_sources, archive_inventory, search_records,
                                  generate_report, queue_archive, check_queue, read_staged,
                                  process_queue, ingest_saved, vin_decode, search_business
  entry(operation, params)     -- 5 operations: create_entry, update_entry, session_start,
                                  session_audit, phase
  core(operation, params)      -- 22 operations: run_script, run_python, run_command,
                                  list_scripts, read_file, write_file, read_document,
                                  neo4j_query, server_info, restart_server, registry_sync,
                                  sync_system_docs, backup_graph, session_ingest,
                                  session_costs, session_search, dispatch_health,
                                  task_status, session_health, backfill_discusses,
                                  harvest_session, watcher_status, session_recover
  query(cypher, database)      -- direct Neo4j Cypher shortcut

No params = execute with defaults. Missing required params -> error + auto-docs.
Explicit docs: pass {"_docs": true} for operation docs, or operation="help" for group docs.

v3.0.0 changes (from v2.0.0):
  - Phase 1: Unified graph interface (write_engine + read_patterns in lib/)
  - Phase 2: Live audit watcher daemon (audit_watcher in lib/, watcher_status tool)
  - Phase 3: Session recovery (session_recover tool), fetch_page optimizations
             (paywall early-exit, SPA escalation, start_tier parameter)
  - Phase 4: Removed 7 deprecated graph operations (add_person, add_organization,
             add_event, add_document, add_property, connect_entities, graph_network).
             PATTERNS.log schema dedup. Documentation updates.
  - lib/ expanded: 19 modules across 3 dependency layers
  - No inline tool logic in server.py

Usage:
  python server.py              # stdio mode (for Claude Desktop)
  python server.py --test       # self-test mode
"""

import sys
import os
import json
from pathlib import Path

# -- Load .env file if present (before any lib/ imports that read env vars) --
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())
    del _line, _key, _val
del _env_file

# Suppress requests' overly cautious dependency version warnings
# (urllib3 2.6.3 and chardet 6.0.0 are compatible but newer than requests 2.32.5 tested against)
import warnings
warnings.filterwarnings("ignore", message="urllib3.*or chardet.*doesn't match a supported version")
del warnings
import asyncio
import subprocess
import traceback
import tempfile
import importlib
import re
import time
import concurrent.futures
from typing import Optional
from datetime import datetime

from mcp.server.fastmcp import FastMCP

# Add nicktools_mcp/ and tools/ to import path for lib/ modules and tool scripts
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent / "tools"))
from lib.paths import CLAUDE_FILES, SCRIPTS_DIR, LIFESTREAM_DIR, ARCHIVES_DIR, OUTPUT_DIR, BROWSE_STATE_DIR
from lib.browsing import BROWSE_RATE_FILE, BROWSE_CACHE_DIR, BROWSE_DEFAULT_DELAY, BROWSE_CACHE_TTL, BROWSE_MAX_RETRIES
from lib.archives import ARCHIVE_MIN_TEXT_SIZE

SERVER_NAME = "nicktools"
SERVER_VERSION = "3.0.0"

# TOOLS_DIR is server-relative (not a shared constant -- only server uses it)
TOOLS_DIR = Path(__file__).parent / "tools"

mcp = FastMCP(SERVER_NAME)

# Thread pool for running sync operations without blocking the event loop
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)


# ============================================================
# Tool Registry: maps operations to scripts + config
# ============================================================
# Each operation has:
#   "script": path relative to tools/ dir
#   "impl": (module_path, func_name) for in-process dispatch (or None for subprocess-only)
#   "timeout": default timeout in seconds
#   "max_timeout": maximum allowed timeout
#   "inject": dict of extra params to inject (config values, paths)
#   "preprocess": optional callable(params) -> params for validation/transformation

VALID_ENTRY_TYPES = {"idea", "finding", "decision", "question", "connection",
                     "artifact", "session", "milestone", "analysis", "reflection", "draft"}
VALID_REL_TYPES = {"EMPLOYED_BY", "WORKED_AT", "AFFILIATED_WITH", "COLLABORATED_WITH",
                   "PART_OF", "FAMILY_OF", "INVOLVED_IN", "RESOLVES_TO", "MEMBER_OF", "OCCURRED_AT"}


def _parse_json_fields(params, list_keys=(), dict_keys=()):
    """Parse JSON string fields in params dict to their native types.

    Args:
        params: The params dict to mutate in place.
        list_keys: Keys whose values should be parsed as JSON arrays (default to []).
        dict_keys: Keys whose values should be parsed as JSON objects (default to {}).

    Returns:
        The mutated params dict.
    """
    for key in list_keys:
        v = params.get(key)
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                params[key] = parsed if isinstance(parsed, list) else [parsed]
            except (json.JSONDecodeError, ValueError):
                # Fallback: treat as comma-separated string
                params[key] = [s.strip() for s in v.split(",") if s.strip()]
        elif v is None:
            pass  # Leave absent keys absent
    for key in dict_keys:
        v = params.get(key)
        if isinstance(v, str):
            params[key] = json.loads(v) if v else {}
        elif v is None:
            pass
    return params


def _preprocess_browse_url(params):
    """Inject browse config and ensure state dirs exist."""
    BROWSE_STATE_DIR.mkdir(parents=True, exist_ok=True)
    BROWSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    params.setdefault("default_delay", BROWSE_DEFAULT_DELAY)
    params.setdefault("cache_ttl", BROWSE_CACHE_TTL)
    params.setdefault("max_retries", BROWSE_MAX_RETRIES)
    params.setdefault("rate_file", str(BROWSE_RATE_FILE))
    params.setdefault("cache_dir", str(BROWSE_CACHE_DIR))
    return params


def _preprocess_archive_source(params):
    """Inject archive config and parse tags."""
    _parse_json_fields(params, list_keys=("tags",))
    params.setdefault("archives_dir", str(ARCHIVES_DIR))
    params.setdefault("min_text_size", ARCHIVE_MIN_TEXT_SIZE)
    return params


def _preprocess_save_page(params):
    """Parse tags for save_page."""
    _parse_json_fields(params, list_keys=("tags",))
    return params


def _preprocess_extract_saved_article(params):
    """Inject archives dir and parse tags."""
    _parse_json_fields(params, list_keys=("tags",))
    params.setdefault("archives_dir", str(ARCHIVES_DIR))
    return params


def _preprocess_archive_inventory(params):
    """Inject archives dir."""
    params.setdefault("archives_dir", str(ARCHIVES_DIR))
    return params


def _preprocess_generate_report(params):
    """Parse entities list and inject output dir."""
    _parse_json_fields(params, list_keys=("entities",))
    params.setdefault("output_dir", str(CLAUDE_FILES / "reports"))
    return params


def _preprocess_neo4j_query(params):
    """Parse params string to dict if needed."""
    _parse_json_fields(params, dict_keys=("params",))
    params.setdefault("params", {})
    return params


def _preprocess_create_entry(params):
    """Validate entry type, parse JSON string params, inject paths."""
    et = params.get("entry_type", "finding")
    if et not in VALID_ENTRY_TYPES:
        raise ValueError(f"Invalid entry_type '{et}'. Must be one of: {', '.join(sorted(VALID_ENTRY_TYPES))}")
    _parse_json_fields(params, list_keys=("domains", "tags", "sources", "discusses"), dict_keys=("links",))
    params.setdefault("lifestream_dir", str(LIFESTREAM_DIR))
    return params


def _preprocess_update_entry(params):
    """Validate entry type if provided, parse JSON string params, inject paths."""
    et = params.get("entry_type")
    if et is not None and et not in VALID_ENTRY_TYPES:
        raise ValueError(f"Invalid entry_type '{et}'. Must be one of: {', '.join(sorted(VALID_ENTRY_TYPES))}")
    _parse_json_fields(params, list_keys=("domains", "tags", "add_discusses", "remove_discusses"),
                       dict_keys=("add_links", "remove_links"))
    params.setdefault("lifestream_dir", str(LIFESTREAM_DIR))
    return params


def _preprocess_session_audit(params):
    """Inject session date default and paths."""
    if not params.get("session_date"):
        params["session_date"] = datetime.now().strftime("%Y-%m-%d")
    params.setdefault("lifestream_dir", str(LIFESTREAM_DIR))
    params.setdefault("min_text_size", ARCHIVE_MIN_TEXT_SIZE)
    return params


## _preprocess_connect_entities, _preprocess_graph_tool -- removed in v3 Phase 4


def _preprocess_node_ops(params):
    """Validate and parse params for generic node operations."""
    action = params.get("action")
    if not action:
        raise ValueError("Missing required parameter 'action'. Must be: add, update, or get")
    if action not in ("add", "update", "get"):
        raise ValueError(f"Invalid action '{action}'. Must be: add, update, or get")
    label = params.get("label")
    if not label:
        raise ValueError("Missing required parameter 'label'. E.g. 'Agent', 'Person', 'Organization'")
    # Parse JSON string props if passed as string
    props = params.get("props")
    if isinstance(props, str):
        props = json.loads(props)
    # Flatten props into top-level kwargs (node_impl expects flat kwargs)
    if isinstance(props, dict):
        del params["props"]
        params.update(props)
    return params


def _preprocess_rel_ops(params):
    """Validate and parse params for generic relationship operations.

    Supports shorthand aliases: from/to/rel → from_name/to_name/type
    Action defaults to "add" if omitted.
    """
    # --- Alias resolution: from/to/rel → from_name/to_name/type ---
    if "rel" in params and "type" not in params:
        params["type"] = params.pop("rel")
    elif "rel" in params:
        params.pop("rel")  # ignore duplicate

    if "from" in params and "from_name" not in params:
        params["from_name"] = params.pop("from")
    elif "from" in params:
        params.pop("from")

    if "to" in params and "to_name" not in params:
        params["to_name"] = params.pop("to")
    elif "to" in params:
        params.pop("to")

    # Default action to "add"
    action = params.get("action", "add")
    params["action"] = action
    if action not in ("add", "update", "remove"):
        raise ValueError(f"Invalid action '{action}'. Must be: add, update, or remove")
    if not params.get("type"):
        raise ValueError("Missing required parameter 'type' (or 'rel'). E.g. 'EMPLOYED_BY', 'FAMILY_OF'")
    if not params.get("from_name"):
        raise ValueError("Missing required parameter 'from_name' (or 'from')")
    if not params.get("to_name"):
        raise ValueError("Missing required parameter 'to_name' (or 'to')")
    # Parse JSON string props if passed as string
    props = params.get("props")
    if isinstance(props, str):
        params["props"] = json.loads(props)
    return params


def _preprocess_wire_evidence(params):
    """Validate and parse params for wire_evidence operations."""
    if not params.get("entity"):
        raise ValueError("Missing required parameter 'entity'. The entity name to wire evidence to.")
    _parse_json_fields(params, list_keys=("sources",), dict_keys=("extra_params",))
    return params


def _preprocess_commit(params):
    """Parse operations array for batch commit."""
    ops = params.get("operations")
    if isinstance(ops, str):
        params["operations"] = json.loads(ops)
    ops = params.get("operations")
    if not ops or not isinstance(ops, list):
        raise ValueError("Missing or invalid 'operations' parameter. Must be a JSON array of {op, ...} dicts.")
    # Parse any nested JSON strings within individual operations
    for op in ops:
        if isinstance(op, dict):
            for key in ("props", "sources", "extra_params"):
                v = op.get(key)
                if isinstance(v, str):
                    op[key] = json.loads(v)
    return params


def _preprocess_board_snapshot(params):
    """Parse members list for board snapshot."""
    _parse_json_fields(params, list_keys=("members",))
    return params


def _preprocess_cypher(params):
    """Parse params for cypher passthrough."""
    _parse_json_fields(params, dict_keys=("params",))
    params.setdefault("params", {})
    return params


def _preprocess_write(params):
    """Parse entities array and nested JSON for unified write."""
    _parse_json_fields(params, list_keys=("entities",))
    entities = params.get("entities")
    if not entities or not isinstance(entities, list):
        raise ValueError("Missing or invalid 'entities' parameter. Must be a JSON array of entity dicts.")
    # Parse nested JSON strings within each entity
    for entity in entities:
        if isinstance(entity, dict):
            for key in ("relationships", "sources", "extra_labels"):
                v = entity.get(key)
                if isinstance(v, str):
                    entity[key] = json.loads(v)
    return params


def _preprocess_read(params):
    """Parse params for unified read."""
    _parse_json_fields(params, dict_keys=("where",))
    # Coerce network depth
    if "network" in params:
        try:
            params["network"] = int(params["network"])
        except (TypeError, ValueError):
            raise ValueError(f"Invalid 'network' value: {params['network']}. Must be integer 1-3.")
    # Coerce include_sources to bool
    if "include_sources" in params:
        v = params["include_sources"]
        if isinstance(v, str):
            params["include_sources"] = v.lower() in ("true", "1", "yes")
    return params


def _preprocess_gds(params):
    """Parse params for GDS operations."""
    # nodes/relationships can be a single string (label/type) or JSON array -- try parse, keep string on failure
    for key in ("nodes", "relationships"):
        v = params.get(key)
        if isinstance(v, str):
            try:
                params[key] = json.loads(v)
            except json.JSONDecodeError:
                pass  # Leave as string -- single label/type
    _parse_json_fields(params, dict_keys=("config",))
    return params


def _preprocess_server_info(params):
    """Inject server context."""
    op_count = sum(len(g["operations"]) for g in TOOL_REGISTRY.values())
    params["_server_context"] = {
        "server_name": SERVER_NAME,
        "server_version": SERVER_VERSION,
        "tools_dir": str(TOOLS_DIR),
        "in_process_tools": list(IN_PROCESS_TOOLS.keys()),
        "operation_count": op_count,
    }
    return params


# The registry: group -> operation -> config
TOOL_REGISTRY = {
    "graph": {
        "usage_file": "tools/graph/USAGE.md",
        "operations": {
            "write":             {"script": "graph/write_ops.py",          "timeout": 60, "max_timeout": 180, "preprocess": _preprocess_write},
            "read":              {"script": "graph/read_ops.py",           "timeout": 25, "max_timeout": 60,  "preprocess": _preprocess_read},
            "node":              {"script": "graph/node_ops.py",           "timeout": 25, "max_timeout": 60, "preprocess": _preprocess_node_ops},
            "rel":               {"script": "graph/rel_ops.py",           "timeout": 25, "max_timeout": 60, "preprocess": _preprocess_rel_ops},
            "wire_evidence":     {"script": "graph/wire_evidence.py",     "timeout": 30, "max_timeout": 60, "preprocess": _preprocess_wire_evidence},
            # add_person, add_organization, add_event, add_document, add_property
            # REMOVED in v3 Phase 4. Use graph("write", ...) or graph("node", ...) instead.
            # Files kept as stubs for reference.
            "commit":            {"script": "graph/commit_ops.py",         "timeout": 60, "max_timeout": 180, "preprocess": _preprocess_commit},
            "cypher":            {"script": "graph/cypher_passthrough.py", "timeout": 30, "max_timeout": 120, "preprocess": _preprocess_cypher},
            "gds":               {"script": "graph/gds_ops.py",           "timeout": 60, "max_timeout": 180, "preprocess": _preprocess_gds},
            "board_snapshot":    {"script": "graph/board_snapshot.py",    "timeout": 30, "max_timeout": 60, "preprocess": _preprocess_board_snapshot},
            "deduplicate":       {"script": "graph/dedup_ops.py",         "timeout": 30, "max_timeout": 60},
            "audit":             {"script": "graph/audit_ops.py",        "timeout": 30, "max_timeout": 60},
            "bulk_update":       {"script": "graph/bulk_update_ops.py",  "timeout": 60, "max_timeout": 180},
            # connect_entities, graph_network
            # REMOVED in v3 Phase 4. Use graph("rel", ...) and graph("read", ...) instead.
        }
    },
    "research": {
        "usage_file": "tools/research/USAGE.md",
        "operations": {
            "browse_url":        {"script": "research/browse_url.py",        "timeout": 60,  "max_timeout": 120, "preprocess": _preprocess_browse_url},
            "archive_source":    {"script": "research/archive_source.py",    "timeout": 120, "max_timeout": 180, "preprocess": _preprocess_archive_source},
            "save_page":         {"script": "research/save_page.py",         "timeout": 120, "max_timeout": 180, "preprocess": _preprocess_save_page},
            "extract_saved_article": {"script": "research/extract_article.py", "timeout": 30, "max_timeout": 60, "preprocess": _preprocess_extract_saved_article},
            "extract_article":       {"script": "research/extract_article.py", "timeout": 30, "max_timeout": 60, "preprocess": _preprocess_extract_saved_article, "impl_func": "extract_saved_article_impl"},  # deprecated alias
            "search_pdf":        {"script": "research/search_pdf.py",        "timeout": 60,  "max_timeout": 120},
            "wayback_lookup":    {"script": "research/wayback_lookup.py",    "timeout": 15,  "max_timeout": 30},
            "read":              {"script": "research/read.py",             "timeout": 120, "max_timeout": 300},
            "archive":           {"script": "research/archive.py",          "timeout": 120, "max_timeout": 300},
            "fetch_page":        {"script": "research/fetch_page.py",       "timeout": 60,  "max_timeout": 120},
            "check_sources":     {"script": "research/check_sources.py",     "timeout": 60,  "max_timeout": 120},
            "archive_inventory": {"script": "research/archive_inventory.py", "timeout": 30,  "max_timeout": 60,  "preprocess": _preprocess_archive_inventory},
            "search_records":    {"script": "research/search_records.py",    "timeout": 30,  "max_timeout": 60},
            "vin_decode":        {"script": "research/vin_decode.py",        "timeout": 30,  "max_timeout": 60},
            "search_business":   {"script": "research/search_business.py",   "timeout": 30,  "max_timeout": 60},
            "generate_report":   {"script": "research/generate_report.py",   "timeout": 60,  "max_timeout": 120, "preprocess": _preprocess_generate_report},
            "queue_archive":     {"script": "research/queue_archive.py",     "timeout": 10,  "max_timeout": 30},
            "check_queue":       {"script": "research/check_queue.py",       "timeout": 10,  "max_timeout": 30},
            "read_staged":       {"script": "research/read_staged.py",       "timeout": 10,  "max_timeout": 30},
            "process_queue":     {"script": "research/process_queue.py",     "timeout": 300, "max_timeout": 600},
            "ingest_saved":      {"script": "research/ingest_saved.py",      "timeout": 30,  "max_timeout": 60},
        }
    },
    "entry": {
        "usage_file": "tools/workflow/USAGE.md",
        "operations": {
            "create_entry":  {"script": "workflow/create_entry.py",  "timeout": 30, "max_timeout": 60, "preprocess": _preprocess_create_entry},
            "update_entry":  {"script": "workflow/update_entry.py",  "timeout": 30, "max_timeout": 60, "preprocess": _preprocess_update_entry},
            "session_start": {"script": "workflow/session_start.py", "timeout": 30, "max_timeout": 60},
            "session_audit": {"script": "workflow/session_audit.py", "timeout": 30, "max_timeout": 60, "preprocess": _preprocess_session_audit},
            "phase":         {"script": "workflow/phase_ops.py",   "timeout": 30, "max_timeout": 60},
        }
    },
    "core": {
        "usage_file": "tools/core/USAGE.md",
        "operations": {
            "run_script":    {"script": "core/run_script.py",    "timeout": 60, "max_timeout": 300},
            "run_python":    {"script": "core/run_python.py",    "timeout": 60, "max_timeout": 300},
            "run_command":   {"script": "core/run_command.py",   "timeout": 30, "max_timeout": 120},
            "list_scripts":  {"script": "core/list_scripts.py",  "timeout": 30, "max_timeout": 60},
            "read_file":     {"script": "core/read_file.py",     "timeout": 30, "max_timeout": 60},
            "write_file":    {"script": "core/write_file.py",    "timeout": 30, "max_timeout": 60},
            "read_document": {"script": "core/read_document.py", "timeout": 60, "max_timeout": 120},
            "neo4j_query":   {"script": "core/neo4j_query.py",   "timeout": 25, "max_timeout": 120, "preprocess": _preprocess_neo4j_query},
            "server_info":     {"script": "core/server_info.py",     "timeout": 10, "max_timeout": 30,  "preprocess": _preprocess_server_info},
            "restart_server":  {"script": "core/restart_server.py",  "timeout": 10, "max_timeout": 30},
            "registry_sync":   {"script": "core/registry_sync.py",   "timeout": 30, "max_timeout": 60},
            "sync_system_docs": {"script": "core/sync_system_docs.py", "timeout": 30, "max_timeout": 60},
            "backup_graph":     {"script": "core/backup_graph.py",     "timeout": 60, "max_timeout": 180},
            "session_ingest":   {"script": "core/session_ingest.py",   "timeout": 120, "max_timeout": 300},
            "session_costs":    {"script": "core/session_costs.py",    "timeout": 60,  "max_timeout": 120},
            "session_search":   {"script": "core/session_search.py",   "timeout": 60,  "max_timeout": 120},
            "dispatch_health":  {"script": "core/dispatch_health.py",  "timeout": 10,  "max_timeout": 30},
            "task_status":      {"script": "core/task_status.py",      "timeout": 10,  "max_timeout": 30},
            "session_health":   {"script": "core/session_health.py",   "timeout": 10,  "max_timeout": 30},
            "backfill_discusses": {"script": "core/backfill_discusses.py", "timeout": 120, "max_timeout": 300},
            "harvest_session":   {"script": "core/harvest_session.py",   "timeout": 120, "max_timeout": 300},
            "watcher_status":    {"script": "core/watcher_status.py",    "timeout": 5,   "max_timeout": 10},
            "session_recover":   {"script": "core/session_recover.py",   "timeout": 15,  "max_timeout": 30},
            "schema_info":       {"script": "core/schema_info.py",       "timeout": 30,  "max_timeout": 60},
        }
    },
}

# Build IN_PROCESS_TOOLS from registry (maps script path -> module info for importlib dispatch)
# Subprocess-only tools: nodriver-based tools + consolidated tools that spawn nested
# subprocesses (deadlock in-process due to MCP server pipe handle inheritance on Windows)
SUBPROCESS_ONLY = {
    "research/browse_url.py", "research/archive_source.py",
    "research/save_page.py", "research/check_sources.py",
    "research/read.py", "research/archive.py",
}

IN_PROCESS_TOOLS = {}
for _group in TOOL_REGISTRY.values():
    for _op_name, _op_config in _group["operations"].items():
        _script = _op_config["script"]
        if _script not in SUBPROCESS_ONLY:
            # Derive module path and impl function name from script path
            # e.g. "graph/add_person.py" -> ("graph.add_person", "add_person_impl")
            # Allow explicit impl_func override for aliases (multiple ops -> same script)
            _module = _script.replace("/", ".").replace(".py", "")
            _func = _op_config.get("impl_func") or (_op_name + "_impl")
            IN_PROCESS_TOOLS[_script] = (_module, _func)


# Shared Neo4j driver -- eagerly created at module scope (before asyncio starts).
# Neo4j Python driver v5+ uses asyncio internally; creating a driver inside a
# ThreadPoolExecutor thread deadlocks when a running event loop exists.
# Pre-creating here avoids the conflict entirely.
from lib.db import get_neo4j_driver as _get_neo4j_driver, ensure_apoc_triggers as _ensure_triggers, ENTRY_DATABASE
_shared_driver = _get_neo4j_driver()

# Auto-repair APOC triggers on startup -- prevents silent breakage after Neo4j restarts.
try:
    _trigger_status = _ensure_triggers(driver=_shared_driver)
    _trigger_msg = ", ".join(f"{k}={v}" for k, v in _trigger_status.items())
    print(f"[startup] APOC triggers: {_trigger_msg}", file=__import__('sys').stderr)
except Exception as _e:
    print(f"[startup] APOC trigger check failed: {_e}", file=__import__('sys').stderr)

# Auto-detect current Cowork session on startup.
# Caches result in lib/session_detect for use by create_entry and dispatch_health.
try:
    from lib.session_detect import detect_current_session as _detect_session
    _current_session = _detect_session()
    if _current_session:
        _proc = _current_session.get("processName", "?")
        _title = _current_session.get("title", "untitled")[:50]
        print(f"[startup] Session: {_proc} | {_title}", file=__import__('sys').stderr)
    else:
        print("[startup] No Cowork session detected", file=__import__('sys').stderr)
except Exception as _e:
    print(f"[startup] Session detection failed: {_e}", file=__import__('sys').stderr)

# Live audit watcher -- background thread monitoring audit.jsonl in real-time.
# Extracts session metadata, entity names, source URLs -> writes to CoworkSession node.
try:
    from lib.audit_watcher import start_watcher as _start_watcher
    if _current_session and _current_session.get("auditPath"):
        _start_watcher(
            audit_path=_current_session["auditPath"],
            session_id=_current_session.get("sessionId"),
            process_name=_current_session.get("processName"),
            driver=_shared_driver,
        )
    else:
        print("[startup] No audit path -- watcher not started", file=__import__('sys').stderr)
except Exception as _e:
    print(f"[startup] Audit watcher failed to start: {_e}", file=__import__('sys').stderr)


# ============================================================
# In-Process Dispatch: _impl() functions called directly
# ============================================================

def _run_impl(module_path, func_name, params):
    """Run an _impl() function in the thread pool.

    Imports the module with reload() so edits take effect without restart.
    Injects the shared Neo4j driver so _impl() never creates its own.
    Returns JSON string for dict results, plain string for string results.
    """
    try:
        module = importlib.import_module(module_path)
        importlib.reload(module)
        impl_func = getattr(module, func_name)
        # Inject shared driver -- _impl() functions accept driver= kwarg
        params_with_driver = {**params, "driver": _shared_driver}
        result = impl_func(**params_with_driver)
        # String results pass through directly; dicts/lists get JSON-encoded
        if isinstance(result, str):
            return result
        return json.dumps(result, indent=2, default=str)
    except Exception:
        tb = traceback.format_exc()
        return json.dumps({
            "error": f"In-process call failed: {module_path}.{func_name}",
            "traceback": tb
        }, indent=2)


async def _call_tool_in_process(script_name, params):
    """Call a tool's _impl() function in-process via thread pool."""
    module_path, func_name = IN_PROCESS_TOOLS[script_name]
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, _run_impl, module_path, func_name, params)


# ============================================================
# Infrastructure: subprocess runner + tool dispatcher
# ============================================================

def _run_subprocess(cmd, timeout, cwd=None):
    """Sync subprocess runner -- called from thread pool.

    Uses temp files for stdout/stderr to avoid pipe deadlocks on Windows.
    """
    stdout_file = tempfile.NamedTemporaryFile(mode='w', suffix='_out.txt', delete=False, dir=str(OUTPUT_DIR))
    stderr_file = tempfile.NamedTemporaryFile(mode='w', suffix='_err.txt', delete=False, dir=str(OUTPUT_DIR))

    try:
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            timeout=timeout,
            cwd=cwd,
            creationflags=subprocess.CREATE_NO_WINDOW
        )

        stdout_file.close()
        stderr_file.close()

        stdout_text = Path(stdout_file.name).read_text(encoding='utf-8', errors='replace')
        stderr_text = Path(stderr_file.name).read_text(encoding='utf-8', errors='replace')

        result_parts = []
        if stdout_text.strip():
            result_parts.append(stdout_text)
        if stderr_text.strip():
            result_parts.append(f"[STDERR]\n{stderr_text}")
        if proc.returncode != 0:
            result_parts.append(f"[EXIT CODE: {proc.returncode}]")
        return "\n".join(result_parts) if result_parts else "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: Timed out after {timeout}s"
    except Exception as e:
        return f"ERROR: {traceback.format_exc()}"
    finally:
        try:
            os.unlink(stdout_file.name)
        except:
            pass
        try:
            os.unlink(stderr_file.name)
        except:
            pass


async def _call_tool(script_name: str, params: dict, timeout: int = 30) -> str:
    """Call a tool -- in-process if available, subprocess otherwise.

    In-process: imports _impl() via importlib.reload, runs in thread pool.
    Subprocess: writes params to temp JSON, spawns python process.
    """
    if script_name in IN_PROCESS_TOOLS:
        return await _call_tool_in_process(script_name, params)
    return await _call_tool_subprocess(script_name, params, timeout)


async def _call_tool_subprocess(script_name: str, params: dict, timeout: int = 30) -> str:
    """Call a tool script via subprocess with params passed as temp JSON file."""
    params_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='_params.json', delete=False,
        dir=str(OUTPUT_DIR), encoding='utf-8'
    )
    json.dump(params, params_file, ensure_ascii=False)
    params_file.close()

    script_path = TOOLS_DIR / script_name

    try:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _executor,
            _run_subprocess,
            [sys.executable, str(script_path), params_file.name],
            timeout,
            None
        )
    finally:
        try:
            os.unlink(params_file.name)
        except:
            pass


# ============================================================
# Auto-generated discovery docs (replaces USAGE.md reads)
# ============================================================

def _generate_operation_docs(group: str, operation: str, op_config: dict) -> str:
    """Auto-generate documentation for a single operation from code introspection.

    Reads the tool's .py file, parses AST for function signature + docstring,
    extracts frontmatter metadata, and formats into a compact doc string.
    No USAGE.md needed -- the code IS the documentation.
    """
    try:
        from tools.core.registry_sync import _extract_tool_metadata
    except ImportError:
        return f"[introspection unavailable for '{operation}']"

    script_name = op_config["script"]
    tool_path = TOOLS_DIR / script_name

    if not tool_path.exists():
        return f"Tool file not found: {script_name}"

    meta = _extract_tool_metadata(tool_path)
    if "error" in meta:
        return f"Parse error for {operation}: {meta['error']}"

    lines = [f"## {operation}"]

    # Deprecation notice
    if op_config.get("deprecated"):
        use_instead = op_config.get("deprecated_use", "")
        lines.append(f"**[!] DEPRECATED** -- Use {use_instead} instead." if use_instead else "**[!] DEPRECATED**")
        lines.append("")

    # Description from frontmatter or first line of module doc
    fm = meta.get("frontmatter", {})
    desc = fm.get("description", "")
    if not desc and meta.get("module_doc"):
        desc = meta["module_doc"].split("\n")[0].split("---")[0].strip()
    if desc:
        lines.append(desc)
    lines.append("")

    # Parameters table
    params = meta.get("params", [])
    if params:
        lines.append("**Parameters:**")
        for p in params:
            req = "required" if p["required"] else f"default: {p['default']}"
            lines.append(f"  - `{p['name']}` ({req})")
        lines.append("")

    # Function docstring (Args section)
    func_doc = meta.get("func_doc", "")
    if func_doc:
        # Extract just the Args section if present
        args_match = re.search(r'Args:\s*\n(.*?)(?:\n\s*\n|\n\s*Returns:|\Z)',
                               func_doc, re.DOTALL)
        if args_match:
            lines.append("**Param details:**")
            lines.append(args_match.group(1).rstrip())
            lines.append("")

    # Graph metadata from frontmatter
    if fm.get("creates_nodes"):
        lines.append(f"Creates: {', '.join(fm['creates_nodes'])}")
    if fm.get("creates_edges"):
        lines.append(f"Edges: {', '.join(fm['creates_edges'])}")
    if fm.get("databases"):
        lines.append(f"Database: {', '.join(fm['databases'])}")

    return "\n".join(lines)


def _generate_group_docs(group: str) -> str:
    """Auto-generate full documentation for a tool group from code introspection.

    Replaces the hand-maintained USAGE.md files. Introspects every tool in the
    group and assembles a compact reference.
    """
    group_config = TOOL_REGISTRY.get(group)
    if not group_config:
        return f"Unknown group: {group}"

    operations = group_config["operations"]
    lines = [f"# {group} -- {len(operations)} operations", ""]

    # Quick reference: operation -> required params
    lines.append("**Quick reference:**")
    for op_name, op_config in sorted(operations.items()):
        deprecated_tag = " [!] DEPRECATED" if op_config.get("deprecated") else ""
        try:
            from tools.core.registry_sync import _extract_tool_metadata
            tool_path = TOOLS_DIR / op_config["script"]
            meta = _extract_tool_metadata(tool_path)
            required = [p["name"] for p in meta.get("params", []) if p["required"]]
            optional = [p["name"] for p in meta.get("params", []) if not p["required"]]
            req_str = ", ".join(required) if required else "(none required)"
            opt_str = f" [optional: {', '.join(optional)}]" if optional else ""
            lines.append(f"  - `{op_name}({req_str})`{opt_str}{deprecated_tag}")
        except Exception:
            lines.append(f"  - `{op_name}` (introspection failed){deprecated_tag}")
    lines.append("")

    # Full docs per operation
    for op_name in sorted(operations.keys()):
        lines.append(_generate_operation_docs(group, op_name, operations[op_name]))
        lines.append("")

    return "\n".join(lines)


def _extract_usage_section(usage_text: str, operation: str) -> str:
    """Extract a specific operation's section from a USAGE.md file.

    LEGACY -- kept as fallback if code introspection fails.
    """
    pattern = rf'^## {re.escape(operation)}\s*$'
    match = re.search(pattern, usage_text, re.MULTILINE)
    if not match:
        return f"No documentation found for operation '{operation}'."

    start = match.start()
    next_header = re.search(r'^## ', usage_text[match.end():], re.MULTILINE)
    if next_header:
        end = match.end() + next_header.start()
    else:
        end = len(usage_text)

    return usage_text[start:end].strip()


# ============================================================
# Generic Dispatcher
# ============================================================

async def _dispatch(group: str, operation: str, params_json: str) -> str:
    """Smart dispatcher: route operation to tool module.

    - If operation = "help": return full USAGE.md
    - If no params (discovery mode): return USAGE.md section for the operation
    - If params provided and valid: execute and return results
    - If params provided but invalid: return error + relevant docs
    - If operation not found: return list of valid operations
    """
    group_config = TOOL_REGISTRY.get(group)
    if not group_config:
        return json.dumps({"error": f"Unknown group '{group}'. Valid groups: {sorted(TOOL_REGISTRY.keys())}"})

    operations = group_config["operations"]
    usage_file = group_config["usage_file"]

    # Help mode: auto-generated group docs from code introspection
    if operation == "help":
        return _generate_group_docs(group)

    # Unknown operation
    if operation not in operations:
        op_list = ", ".join(sorted(operations.keys()))
        return json.dumps({
            "error": f"Unknown operation '{operation}' in {group} group.",
            "valid_operations": sorted(operations.keys()),
            "hint": f"Call with operation='help' for full documentation. Valid operations: {op_list}"
        })

    op_config = operations[operation]

    # Parse params: empty/missing -> empty dict (execute with defaults)
    if not params_json or params_json.strip() == "":
        params = {}
    else:
        try:
            params = json.loads(params_json)
        except json.JSONDecodeError as e:
            docs = "\n\n" + _generate_operation_docs(group, operation, op_config)
            return json.dumps({
                "error": f"Invalid JSON in params: {e}",
                "hint": "params must be a valid JSON object string"
            }) + docs

        if not isinstance(params, dict):
            return json.dumps({"error": "params must be a JSON object (dict), not a list or scalar"})

    # Explicit docs request: {"_docs": true} -> return auto-generated docs
    if params.pop("_docs", False):
        return _generate_operation_docs(group, operation, op_config)

    # Apply timeout from params or use defaults
    timeout = params.pop("timeout_seconds", op_config["timeout"])
    timeout = min(timeout, op_config["max_timeout"])

    # Run preprocess if defined -- validation errors include docs for guidance
    preprocess = op_config.get("preprocess")
    if preprocess:
        try:
            params = preprocess(params)
        except (ValueError, json.JSONDecodeError) as e:
            docs = _generate_operation_docs(group, operation, op_config)
            return json.dumps({"error": str(e)}) + "\n\n" + docs

    # Dispatch to tool (with pattern logging + call monitoring)
    script_name = op_config["script"]
    t0 = time.time()
    try:
        result = await _call_tool(script_name, params, timeout)
        duration_ms = (time.time() - t0) * 1000
        try:
            from lib.patterns import log_pattern
            log_pattern(group, operation, params, result, duration_ms, error=False)
        except Exception:
            pass  # Never let logging break a tool call
        # Record in call monitor -- detect soft errors in result content
        try:
            from lib.call_monitor import record_call, get_dispatch_warning
            # Detect soft errors: tools that return error messages without raising
            call_status = "ok"
            error_key = None
            if isinstance(result, str):
                # Check for JSON {"error": "..."} pattern
                try:
                    rdict = json.loads(result)
                    if isinstance(rdict, dict) and "error" in rdict:
                        call_status = "error"
                        error_key = str(rdict["error"])[:100]
                except (json.JSONDecodeError, TypeError):
                    pass
                # Check for plain "ERROR: ..." prefix
                if call_status == "ok" and result.startswith("ERROR:"):
                    call_status = "error"
                    error_key = result[:100]
            record_call(group, operation, params, call_status, duration_ms, error_key=error_key)
            warning = get_dispatch_warning(operation)
            if warning and isinstance(result, str):
                try:
                    result_dict = json.loads(result)
                    if isinstance(result_dict, dict):
                        result_dict["dispatch_warning"] = warning
                        result = json.dumps(result_dict)
                except (json.JSONDecodeError, TypeError):
                    pass  # Non-JSON result, skip warning injection
        except Exception:
            pass  # Never let monitoring break a tool call
        # If result contains a missing-param error, append docs for guidance
        if isinstance(result, str) and "missing" in result.lower() and "required" in result.lower():
            try:
                docs = _generate_operation_docs(group, operation, op_config)
                result = result + "\n\n" + docs
            except Exception:
                pass
        return result
    except Exception as e:
        duration_ms = (time.time() - t0) * 1000
        error_result = json.dumps({
            "error": f"Tool execution failed: {e}",
            "traceback": traceback.format_exc()
        })
        try:
            from lib.patterns import log_pattern
            log_pattern(group, operation, params, error_result, duration_ms, error=True)
        except Exception:
            pass
        # Record error in call monitor
        try:
            from lib.call_monitor import record_call
            error_key = str(e)[:100]
            record_call(group, operation, params, "error", duration_ms, error_key=error_key)
        except Exception:
            pass
        return error_result


# ============================================================
# Meta-Tool Declarations (5 tools -- replaces 28 individual declarations)
# ============================================================

@mcp.tool()
async def graph(operation: str, params: str = "") -> str:
    """Knowledge graph operations: create/update entities and relationships.

    Operations: write, read, node, rel, wire_evidence, commit, cypher, gds,
    board_snapshot, deduplicate.

    Call with operation only (no params) to execute with defaults.
    Call with operation="help" for full documentation.

    Args:
        operation: Operation name (e.g. "node") or "help" for full docs
        params: JSON object string with operation parameters (omit for docs)
    """
    return await _dispatch("graph", operation, params)


@mcp.tool()
async def research(operation: str, params: str = "") -> str:
    """Web research, archiving, and source management.

    Operations: browse_url, archive_source, save_page, extract_saved_article,
    search_pdf, wayback_lookup, check_sources, archive_inventory,
    search_records, fetch_page, queue_archive, process_queue, ingest_saved.

    Call with operation only (no params) to get usage docs for that operation.
    Call with operation="help" for full documentation.

    Args:
        operation: Operation name (e.g. "fetch_page") or "help" for full docs
        params: JSON object string with operation parameters (omit for docs)
    """
    return await _dispatch("research", operation, params)


@mcp.tool()
async def entry(operation: str, params: str = "") -> str:
    """Lifestream entry creation, updates, session management, and phase tracking.

    Operations: create_entry, update_entry, session_start, session_audit, phase.

    Call with operation only (no params) to get usage docs for that operation.
    Call with operation="help" for full documentation.

    Args:
        operation: Operation name (e.g. "create_entry") or "help" for full docs
        params: JSON object string with operation parameters (omit for docs)
    """
    return await _dispatch("entry", operation, params)


@mcp.tool()
async def core(operation: str, params: str = "") -> str:
    """Run scripts, read/write files, execute Python and shell commands.

    Operations: run_script, run_python, run_command, list_scripts,
    read_file, write_file, read_document, neo4j_query, server_info, restart_server.

    Call with operation only (no params) to get usage docs for that operation.
    Call with operation="help" for full documentation.

    Args:
        operation: Operation name (e.g. "run_python") or "help" for full docs
        params: JSON object string with operation parameters (omit for docs)
    """
    return await _dispatch("core", operation, params)


@mcp.tool()
async def query(cypher: str, database: str = "", params: str = "", mode: str = "auto") -> str:
    """Direct Neo4j Cypher queries against any database.

    Rich Cypher interface with EXPLAIN-based read/write safety, Node/Relationship
    serialization (with _labels/_type), write counters, and max_records safety.
    Routes through the same implementation as graph("cypher").

    Args:
        cypher: The Cypher query to execute
        database: Database name (defaults to NICKTOOLS_GRAPH_DB env var; use "system" for Neo4j admin)
        params: Optional JSON string of query parameters
        mode: Execution mode — "auto" (default, classifies via EXPLAIN),
              "read" (rejects writes), "write" (allows mutations)
    """
    from lib.db import GRAPH_DATABASE
    query_params = {"query": cypher, "database": database or GRAPH_DATABASE, "mode": mode}
    if params:
        query_params["params"] = json.loads(params)
    return await _dispatch("graph", "cypher", json.dumps(query_params))


@mcp.tool()
async def schema(database: str = "") -> str:
    """Retrieve the schema for any Neo4j database.

    Returns node labels with property types and counts, relationship types,
    and index information. Cached for 5 minutes per database.

    Args:
        database: Database name (defaults to NICKTOOLS_GRAPH_DB env var)
    """
    from lib.db import GRAPH_DATABASE
    return await _dispatch("core", "schema_info", json.dumps({"database": database or GRAPH_DATABASE}))


# ============================================================
# Self-test mode
# ============================================================
async def self_test():
    print(f"=== {SERVER_NAME} v{SERVER_VERSION} Self-Test ===\n")

    print("1. server_info (via core meta-tool):")
    print(await core("server_info"))

    print("\n2. list_scripts (via core meta-tool):")
    r = await core("list_scripts", '{"directory": "nicktools_mcp/tools/core"}')
    print(r[:500])

    print("\n3. run_python (via core meta-tool):")
    print(await core("run_python", '{"code": "import sys; print(f\'Python {sys.version} via meta-tool\')"}'))

    print("\n4. query shortcut (via graph/cypher passthrough):")
    print(await query("RETURN 1 AS test", database=ENTRY_DATABASE))

    print("\n5. session_start (via entry meta-tool):")
    r = await entry("session_start", '{"timeout_seconds": 15}')
    print(r[:600])

    print("\n6. Discovery mode: graph('add_person'):")
    r = await graph("add_person")
    print(r[:400])

    print("\n7. Help mode: graph('help'):")
    r = await graph("help")
    print(f"(USAGE.md: {len(r)} chars)")

    print("\n8. sync_system_docs (reference doc freshness check):")
    r = await core("sync_system_docs", '{}')
    import json as _json
    try:
        sync_data = _json.loads(r) if isinstance(r, str) else r
        sections = sync_data.get("sections", {})
        for name, info in sections.items():
            status = info.get("status", "?") if isinstance(info, dict) else "?"
            print(f"  {name}: {status}")
        warns = sync_data.get("warnings", [])
        if warns:
            print(f"  [!] Warnings: {warns}")
        else:
            print("  No validation warnings")
    except Exception as e:
        print(f"  (parse error: {e})")
        print(f"  Raw: {str(r)[:300]}")

    print("\nSelf-test complete!")


# ============================================================
# Entry point
# ============================================================
if __name__ == "__main__":
    if "--test" in sys.argv:
        asyncio.run(self_test())
    else:
        mcp.run(transport="stdio")
