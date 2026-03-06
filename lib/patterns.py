"""
Layer 0 -- Tool usage pattern logger.

Appends one structured line per tool call to tools/{group}/PATTERNS.log.
Smart filtering extracts the signal (entities created, URLs archived,
errors encountered) instead of dumping raw results.

No internal dependencies beyond paths.py. Called from server.py _dispatch().

Log format (tab-separated, one line per call):
  TIMESTAMP\tOPERATION\tSTATUS\tDURATION_MS\tSIGNAL

SIGNAL varies by group and operation -- see _extract_signal() for rules.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from lib.paths import SCRIPTS_DIR
from lib.db import GRAPH_DATABASE

TOOLS_DIR = SCRIPTS_DIR / "nicktools_mcp" / "tools"

# -- Group-to-directory mapping --
GROUP_DIRS = {
    "graph": "graph",
    "research": "research",
    "entry": "workflow",
    "core": "core",
}

# Maximum signal line length (truncate beyond this)
MAX_SIGNAL_LEN = 500

# Operations to skip logging (noisy, low-value)
SKIP_OPS = {"server_info", "list_scripts", "help"}

# Track which groups have had their schema written this server lifetime
_schema_written = set()


def log_pattern(group: str, operation: str, params: dict,
                result_str: str, duration_ms: float, error: bool = False):
    """Append one structured log line to the group's PATTERNS.log.

    Called from server.py after every successful tool dispatch.
    Failures (exceptions) are logged with status=ERROR.
    On first call per group per server lifetime, writes a schema line.
    """
    if operation in SKIP_OPS:
        return

    group_dir = GROUP_DIRS.get(group)
    if not group_dir:
        return

    log_path = TOOLS_DIR / group_dir / "PATTERNS.log"

    # On first call per group, write a schema separator
    if group not in _schema_written:
        _schema_written.add(group)
        _write_schema(group, group_dir, log_path)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    status = "ERROR" if error else "OK"
    duration = f"{duration_ms:.0f}ms"

    signal = _extract_signal(group, operation, params, result_str, error)
    if len(signal) > MAX_SIGNAL_LEN:
        signal = signal[:MAX_SIGNAL_LEN] + "..."

    line = f"{ts}\t{operation}\t{status}\t{duration}\t{signal}\n"

    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # Never let logging break a tool call


def _write_schema(group: str, group_dir: str, log_path: Path):
    """Write a session schema separator to the group's PATTERNS.log -- only if changed.

    Called once per group per server lifetime (i.e., once per session).
    Lists all operations in the group with their required params.
    Compares a hash of the schema content against the last written hash
    in the log file. If identical, writes only a lightweight session marker.
    This eliminates the ~30-line schema preamble that repeated every session.
    """
    try:
        from tools.core.registry_sync import _extract_tool_metadata
        import importlib
        server = importlib.import_module("server")
        registry = getattr(server, "TOOL_REGISTRY", {})
        group_config = registry.get(group, {})
        operations = group_config.get("operations", {})

        # Build schema lines (without timestamp -- just operations)
        schema_lines = []
        for op_name in sorted(operations.keys()):
            op_config = operations[op_name]
            tool_path = TOOLS_DIR / op_config["script"]
            if not tool_path.exists():
                schema_lines.append(f"# {op_name}: file missing")
                continue

            meta = _extract_tool_metadata(tool_path)
            req_str = ", ".join(p["name"] for p in meta.get("params", []) if p["required"])
            opt_parts = [f"{p['name']}={p['default']}" for p in meta.get("params", [])
                         if not p["required"] and p["name"] not in ("database",)]
            opt_str = f" [{', '.join(opt_parts)}]" if opt_parts else ""
            schema_lines.append(f"# {op_name}({req_str}){opt_str}")

        # Hash the schema content
        schema_body = "\n".join(schema_lines)
        schema_hash = hashlib.md5(schema_body.encode()).hexdigest()[:8]

        # Check if last schema in the log has the same hash
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        last_hash = _read_last_schema_hash(log_path)

        if last_hash == schema_hash:
            # Schema unchanged -- write a lightweight session marker only
            marker = f"\n# -- session {ts} (schema={schema_hash}, unchanged) --\n"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(marker)
        else:
            # Schema changed (or first time) -- write full schema with hash
            header = f"\n# -- session {ts} (schema={schema_hash}) --"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(header + "\n" + schema_body + "\n")

    except Exception:
        pass  # Never let schema writing break a tool call


def _read_last_schema_hash(log_path: Path) -> str:
    """Read the schema hash from the most recent session marker in a PATTERNS.log.

    Looks for lines matching: # -- session YYYY-MM-DD HH:MM:SS (schema=HASH...) --
    Returns the hash string, or empty string if not found.
    """
    try:
        if not log_path.exists():
            return ""
        # Read last 5KB -- schema markers are near the end
        size = log_path.stat().st_size
        with open(log_path, "r", encoding="utf-8") as f:
            if size > 5000:
                f.seek(size - 5000)
                f.readline()  # skip partial line
            tail = f.read()

        # Find all schema= markers, return the last one
        matches = re.findall(r'\(schema=([a-f0-9]+)', tail)
        return matches[-1] if matches else ""
    except Exception:
        return ""


def _extract_signal(group: str, operation: str, params: dict,
                    result_str: str, error: bool) -> str:
    """Extract the important data from a tool result.

    Returns a compact, grep-friendly string. The rules differ by group
    and operation to capture what matters most.
    """
    if error:
        return _extract_error(result_str)

    # Try to parse result as JSON
    result = _safe_parse(result_str)

    # -- Dispatch to group-specific extractors --
    extractors = {
        "graph": _signal_graph,
        "research": _signal_research,
        "entry": _signal_entry,
        "core": _signal_core,
    }
    extractor = extractors.get(group, _signal_default)
    try:
        return extractor(operation, params, result, result_str)
    except Exception as e:
        return f"[signal-extract-failed: {e}]"


def _extract_error(result_str: str) -> str:
    """Pull error message from a failed result."""
    result = _safe_parse(result_str)
    if isinstance(result, dict):
        err = result.get("error", "")
        if err:
            return f"error={err[:200]}"
    # Raw string error
    if result_str:
        first_line = result_str.strip().split("\n")[0]
        return f"error={first_line[:200]}"
    return "error=unknown"


# -- Group-specific signal extractors --

def _signal_graph(operation: str, params: dict, result: dict, raw: str) -> str:
    """Graph tools: entity name, type, relationships wired, sources attached."""
    parts = []

    # Entity name (all graph tools have a name/title param)
    name = params.get("name") or params.get("title") or params.get("address", "")
    if name:
        parts.append(f"entity={name}")

    if isinstance(result, dict):
        # Node created vs merged
        action = result.get("action", result.get("status", ""))
        if action:
            parts.append(f"action={action}")

        # Relationships wired
        rels = result.get("relationships_created") or result.get("edges_created")
        if rels:
            parts.append(f"rels={rels}")

        # Sources wired
        sources = result.get("sources_wired") or result.get("supported_by_count")
        if sources:
            parts.append(f"sources={sources}")

        # Warnings
        warnings = result.get("warnings", [])
        if warnings:
            parts.append(f"warnings={len(warnings)}")

        # For connect_entities
        rel_type = params.get("relationship_type", "")
        from_name = params.get("from_name", "")
        to_name = params.get("to_name", "")
        if rel_type and from_name:
            parts.append(f"{from_name}-[{rel_type}]->{to_name}")

        # For graph_network
        if operation == "graph_network":
            entity_count = result.get("entity_count")
            edge_count = result.get("edge_count")
            if entity_count:
                parts.append(f"entities={entity_count}")
            if edge_count:
                parts.append(f"edges={edge_count}")

    return " | ".join(parts) if parts else _signal_default(operation, params, result, raw)


def _signal_research(operation: str, params: dict, result: dict, raw: str) -> str:
    """Research tools: URL, domain, capture status, extracted chars."""
    parts = []

    url = params.get("url", "")
    if url:
        # Compact URL: domain + path (no query params)
        from urllib.parse import urlparse
        parsed = urlparse(url)
        compact = parsed.netloc + parsed.path.rstrip("/")
        if len(compact) > 80:
            compact = compact[:77] + "..."
        parts.append(f"url={compact}")

    if isinstance(result, dict):
        # Capture status
        capture = result.get("captureStatus") or result.get("capture_status") or result.get("status")
        if capture:
            parts.append(f"capture={capture}")

        # Extracted content length
        chars = result.get("extracted_chars") or result.get("text_length") or result.get("chars")
        if chars:
            parts.append(f"chars={chars}")

        # Title extracted
        title = result.get("title", "")
        if title:
            parts.append(f"title={title[:60]}")

        # Archive path
        archive = result.get("archive_path") or result.get("archivePath")
        if archive:
            parts.append(f"archived={Path(archive).name}")

        # HTTP status
        http_status = result.get("http_status") or result.get("status_code")
        if http_status:
            parts.append(f"http={http_status}")

        # For search_records
        query = params.get("query", "")
        if query and operation == "search_records":
            parts.append(f"query={query[:60]}")

        results_count = result.get("results_count") or result.get("total_results")
        if results_count is not None:
            parts.append(f"results={results_count}")

        # For archive_inventory
        if operation == "archive_inventory":
            dash = result.get("dashboard", result)
            total = dash.get("total_archives") or dash.get("total")
            if total:
                parts.append(f"archives={total}")
            valid = dash.get("total_valid")
            failed = dash.get("total_failed")
            if valid is not None:
                parts.append(f"valid={valid}")
            if failed:
                parts.append(f"failed={failed}")
            ghosts = result.get("reconciliation", {}).get("summary", {}).get("ghost_count")
            if ghosts:
                parts.append(f"ghosts={ghosts}")

        # Source node ID
        source_id = result.get("source_node_id") or result.get("sourceId")
        if source_id:
            parts.append(f"neo4j_id={source_id}")

        # Error / dead
        error = result.get("error")
        if error:
            parts.append(f"error={str(error)[:100]}")

    return " | ".join(parts) if parts else _signal_default(operation, params, result, raw)


def _signal_entry(operation: str, params: dict, result: dict, raw: str) -> str:
    """Entry tools: entry ID, title, type, domains, edges wired."""
    parts = []

    if operation == "session_audit":
        if isinstance(result, dict):
            issue_count = len(result.get("issues", []))
            parts.append(f"issues={issue_count}")
            summary = result.get("summary", {})
            if isinstance(summary, dict):
                for k in ("lifestream_entries", "corcoran_sources", "registry_sync"):
                    v = summary.get(k)
                    if v is not None:
                        parts.append(f"{k}={v}")
        return " | ".join(parts) if parts else _signal_default(operation, params, result, raw)

    if operation == "session_start":
        if isinstance(result, dict):
            # Result may be nested: {sections: {system_pulse: ...}} or flat
            sections = result.get("sections", result)
            pulse = sections.get("system_pulse", {})
            if isinstance(pulse, dict):
                parts.append(f"entries={pulse.get('total_entries', '?')}")
                parts.append(f"sources={pulse.get('total_sources', '?')}")
                parts.append(f"domains={pulse.get('total_domains', '?')}")
            recent = sections.get("recent_entries", [])
            if recent:
                parts.append(f"recent={len(recent)}")
            questions = sections.get("open_questions", [])
            if questions:
                parts.append(f"questions={len(questions)}")
        return " | ".join(parts) if parts else _signal_default(operation, params, result, raw)

    # create_entry / update_entry
    entry_id = ""
    if isinstance(result, dict):
        entry_id = result.get("id") or result.get("entry_id", "")
    title = params.get("title", "")
    entry_type = params.get("entry_type") or params.get("type", "")

    if entry_id:
        parts.append(f"id={entry_id}")
    if title:
        parts.append(f"title={title[:60]}")
    if entry_type:
        parts.append(f"type={entry_type}")

    domains = params.get("domains", [])
    if domains:
        parts.append(f"domains={','.join(domains)}")

    if isinstance(result, dict):
        discusses = result.get("discusses_wired") or result.get("discusses_edges")
        if discusses:
            parts.append(f"discusses={discusses}")
        cites = result.get("cites_wired") or result.get("cites_edges")
        if cites:
            parts.append(f"cites={cites}")

    return " | ".join(parts) if parts else _signal_default(operation, params, result, raw)


def _signal_core(operation: str, params: dict, result: dict, raw: str) -> str:
    """Core tools: script/command, key output, errors."""
    parts = []

    if operation == "neo4j_query":
        cypher = params.get("cypher", "")
        db = params.get("database", GRAPH_DATABASE)
        # First meaningful keyword from cypher
        cypher_compact = cypher.strip().replace("\n", " ")[:80]
        parts.append(f"db={db}")
        parts.append(f"cypher={cypher_compact}")
        if isinstance(result, dict):
            rows = result.get("row_count") or result.get("rows")
            if rows is not None:
                parts.append(f"rows={rows}")
        return " | ".join(parts)

    if operation == "registry_sync":
        if isinstance(result, dict):
            status = result.get("status", "")
            total = result.get("total_operations", "")
            parts.append(f"status={status}")
            parts.append(f"ops={total}")
        return " | ".join(parts) if parts else "ran"

    if operation in ("run_script", "run_python", "run_command"):
        script = params.get("script") or params.get("command", "")
        if script:
            parts.append(f"cmd={script[:80]}")
        # Capture first meaningful output line
        if raw:
            lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
            if lines:
                first = lines[0][:120]
                parts.append(f"out={first}")
        return " | ".join(parts) if parts else "ran"

    if operation == "read_document":
        path = params.get("path", "")
        if path:
            parts.append(f"file={Path(path).name}")
        if isinstance(result, dict):
            chars = result.get("chars") or result.get("length")
            if chars:
                parts.append(f"chars={chars}")
        return " | ".join(parts) if parts else "read"

    if operation in ("read_file", "write_file"):
        path = params.get("path", "")
        if path:
            parts.append(f"file={Path(path).name}")
        return " | ".join(parts) if parts else operation

    return _signal_default(operation, params, result, raw)


def _signal_default(operation: str, params: dict, result: dict, raw: str) -> str:
    """Fallback: dump param keys and result status."""
    parts = []
    if params:
        keys = sorted(k for k in params.keys() if k != "timeout_seconds")
        parts.append(f"params=[{','.join(keys)}]")
    if isinstance(result, dict):
        status = result.get("status") or result.get("action")
        if status:
            parts.append(f"status={status}")
    return " | ".join(parts) if parts else "called"


def _safe_parse(text: str):
    """Try to parse text as JSON. Unwrap {"result": "..."} wrappers.

    In-process tools return {"result": "<json-string>"} where the inner
    value is the actual result. We unwrap to get the meaningful dict.
    """
    if not text or not text.strip():
        return None
    try:
        parsed = json.loads(text.strip())
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return parsed

    # Unwrap {"result": "<json-string>"} wrapper from in-process tools
    if "result" in parsed and isinstance(parsed["result"], str):
        inner = parsed["result"].strip()
        if inner.startswith("{") or inner.startswith("["):
            try:
                return json.loads(inner)
            except (json.JSONDecodeError, TypeError):
                pass
        # Non-JSON string result -- return the wrapper as-is
        return parsed

    return parsed
