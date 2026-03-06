> Auto-generated docs also available: `core("help")` for full reference, or `core("run_python")` (no params) for single-op docs.

# Core Operations

Run scripts, read/write files, execute Python and shell commands, query Neo4j, manage sessions, and monitor system health.

## Return Conventions

Core operations use two return families:

**Dict-returning operations** (most tools): Return a JSON dict. Errors include an `"error"` key. All keys use `snake_case`.

**String-returning operations** (`run_python`, `run_command`, `run_script`, `read_file`, `write_file`, `list_scripts`, `read_document`): Return plain strings -- the raw stdout, file content, or directory listing. Errors use an `"ERROR: ..."` prefix string. This is a deliberate passthrough convention: these tools return content where wrapping in `{"result": "..."}` would add noise without value.

## Routing Guide

- **Quick Python snippet?** -> `run_python` (primary -- captures stdout correctly)
- **Run an existing script?** -> `run_script` (path relative to ClaudeFiles/scripts/)
- **Shell command?** -> `run_command` (PowerShell default, also supports cmd)
- **What scripts are available?** -> `list_scripts`
- **Read a text/config file?** -> `read_file` (Windows paths, max 1MB)
- **Write a file?** -> `write_file` (creates parent dirs automatically)
- **Read a document?** -> `read_document` (auto-detects .docx, .xlsx, .csv, .pdf, .json, .yaml)
- **Direct Cypher query?** -> `neo4j_query` (all 4 databases, parameterized queries)
- **Check server status?** -> `server_info`
- **Restart after lib/ edits?** -> `restart_server`
- **Validate tool registry?** -> `registry_sync`
- **Regenerate reference docs?** -> `sync_system_docs`
- **Export database?** -> `backup_graph`
- **Ingest Cowork sessions?** -> `session_ingest` (historical backfill)
- **Session analytics?** -> `session_costs` (tokens, cost breakdown)
- **Search session transcripts?** -> `session_search` (full-text across audit files)
- **Session health after context reset?** -> `session_health` (error patterns, guidance)
- **Cross-session handoff?** -> `session_recover` (watcher data -> graph state comparison)
- **Harvest session audit?** -> `harvest_session` (extract analytics from audit log)
- **Backfill DISCUSSES edges?** -> `backfill_discusses` (entity name matching)
- **Check call patterns?** -> `dispatch_health` (repetition warnings, error clusters)
- **Background task status?** -> `task_status` (archive queue, SPN, captures)
- **Audit watcher state?** -> `watcher_status` (live session monitoring)

## run_script

Run a Python script on Windows with full stdout/stderr capture. Unlike the Windows MCP Shell, this properly captures all output.

**Parameters:**
- **script_path** (required): Path to .py file (absolute or relative to ClaudeFiles/scripts/)
- **args**: Optional space-separated arguments to pass to the script
- **timeout_seconds**: Max execution time (default 60, max 300)

**Returns:** String -- combined stdout+stderr. Errors prefixed with `"ERROR: "`.

## run_python

Execute inline Python code with full stdout/stderr capture. Writes code to a temp file and runs it. Supports optional Neo4j query injection -- results are pre-fetched and available as JSON files.

**Parameters:**
- **code** (required): Python source code to execute
- **timeout_seconds**: Max execution time (default 60, max 300)
- **queries**: Optional dict mapping names to `{cypher, database}` dicts. Results are injected as `_query_results` dict.

**Examples:**
```json
// Simple execution
core("run_python", {"code": "import sys; print(f'Python {sys.version}')"})

// With Neo4j query injection
core("run_python", {"code": "import json\ndata = json.load(open(_query_results['agents']))\nprint(len(data), 'agents')", "queries": {"agents": {"cypher": "MATCH (a:Agent) RETURN a.name AS name"}}})
```

**Returns:** String -- script stdout. Query results injected as JSON files via `_query_results` dict. Errors prefixed with `"ERROR: "`.

## run_command

Run a shell command with full stdout/stderr capture.

**Parameters:**
- **command** (required): The command to execute
- **shell**: `"powershell"` or `"cmd"` (default: `"powershell"`)
- **timeout_seconds**: Max execution time (default 30, max 120)

**Returns:** String -- command stdout+stderr. Errors prefixed with `"ERROR: "`.

## list_scripts

List available Python scripts that can be run via run_script.

**Parameters:**
- **directory**: Optional subdirectory under ClaudeFiles/scripts/ to list

**Returns:** String -- formatted directory listing with script names and sizes.

## read_file

Read a text file from the Windows filesystem. Max 1MB file size.

**Parameters:**
- **path** (required): Absolute path to the file
- **encoding**: File encoding (default: `"utf-8"`)

**Returns:** String -- file contents. Errors prefixed with `"ERROR: "`.

## write_file

Write content to a file on the Windows filesystem. Creates parent directories automatically.

**Parameters:**
- **path** (required): Absolute path to the file
- **content** (required): Text content to write
- **encoding**: File encoding (default: `"utf-8"`)

**Returns:** String -- confirmation message. Errors prefixed with `"ERROR: "`.

## read_document

Read a document file and return its text content. Auto-detects format by extension.

Supported formats: `.docx`, `.xlsx`, `.csv`, `.tsv`, `.pdf`, `.json`, `.yaml`/`.yml`, `.md`.

**Parameters:**
- **path** (required): Absolute path to the file
- **max_chars**: Maximum characters to return (default 50000)
- **sheet**: For .xlsx -- sheet name to read (default: active sheet)
- **pages**: For .pdf -- page range like `"1-5"` or `"3"` (default: all)

**Returns:** String -- extracted text content. Errors prefixed with `"ERROR: "`.

## neo4j_query

Run a Cypher query against Neo4j and return results as JSON. Supports parameterized queries and all databases.

**Parameters:**
- **cypher** (required): The Cypher query to execute
- **database**: Database name -- `corcoran`, `lifestream`, `planttaxonomy`, `system` (default: `"corcoran"`)
- **params**: JSON string of query parameters

**Example:**
```json
core("neo4j_query", {"cypher": "MATCH (p:Person) WHERE p.name CONTAINS $name RETURN p.name", "params": "{\"name\": \"Kirk\"}"})
```

**Returns:** `{records: [{...}], count: N}` -- list of result records as dicts, plus row count. Or `{error: "..."}`.

## server_info

Return server status, available tools, and environment info. No parameters required.

**Returns:** `{status, version, uptime_seconds, tools: {group: [ops]}, neo4j: {uri, databases}, python_version, startup_time}`.

## restart_server

Restart the MCP server process. Triggers `os._exit(0)` after a brief delay. The MCP client auto-restarts the server, picking up all changes to server.py, TOOL_REGISTRY, lib/ modules, and meta-tool docstrings.

**Parameters:**
- **reason**: Optional reason for restart (included in response)

## registry_sync

Introspect the tool registry, extract metadata from all tool .py files via AST parsing, and validate documentation completeness. Can generate a complete `manifest.json`.

**Parameters:**
- **action**: What to do (default: `"validate"`):
  - `"validate"` -- Check USAGE.md completeness, canonicalization compliance, report drift
  - `"manifest"` -- Generate full manifest.json with all extracted metadata
  - `"report"` -- Human-readable summary of all tools with params and lib deps
- **output_path**: Where to write manifest file (default: `nicktools_mcp/manifest.json`)

## sync_system_docs

Regenerate auto-generated reference documents from live system state.

**Parameters:**
- **sections**: Optional list of sections to regenerate. Default: all.
  Valid values: `["landscape", "schema", "playbooks", "standards"]`

**Reference docs generated:** `system-landscape.md` (from manifest), `neo4j-schema.md` (from live schema queries), `playbook-index.md` (from playbook frontmatter), `research-standards.md` (validated only).

## backup_graph

Export a Neo4j database using APOC export for disaster recovery.

**Parameters:**
- **database**: Database to back up (default: `"corcoran"`)
- **format**: Export format -- `"cypher"` or `"json"` (default: `"cypher"`)

## session_ingest

Ingest Cowork sessions into the lifestream graph. Creates CoworkSession nodes, wires PRECEDED_BY chain, links StreamEntries via PRODUCED_IN, and derives COVERED_TOPIC edges. Idempotent -- safe to run repeatedly.

**Parameters:**
- **mode**: Processing mode (default: `"full"`):
  - `"full"` -- Scan + create + link (complete backfill)
  - `"scan"` -- Inventory only (no graph changes)
  - `"link"` -- Wire edges only (skip node creation)
  - `"auto"` -- Detect current/newest session, upsert that node only (lightweight)
- **process_name**: Optional process name to target in `"auto"` mode

**Returns (scan):** `{session_count, sessions: [{session_id, title, process_name, entry_count, user_message_count, tool_call_count, audit_size_kb}]}`

**Returns (full/link):** `{session_count, nodes_created, nodes_updated, preceded_by_wired, entries_linked, covered_topics_wired, total_sessions, sessions_with_entries, total_linked_entries}`

**Returns (auto):** `{mode, detected, ingested, session_id, process_name, title, model, entry_count, user_message_count, tool_call_count, node_created, node_updated}`

## session_costs

Cost and token analysis across Cowork sessions. Parses audit logs for API usage data.

**Parameters:**
- **session_id**: Optional specific session ID (or `"current"` for active session). If omitted, analyzes all sessions.
- **top_n**: Number of top sessions to return in rankings (default 10)

**Returns:** `{sessions_analyzed, aggregate: {total_cost_usd, total_turns, total_input_tokens, total_output_tokens, cache_hit_rate, ...}, top_sessions_by_cost: [{session_id, title, process_name, cost_usd, turns, input_tokens, output_tokens, duration_minutes, cost_per_turn}], daily_costs: [{date, cost_usd}]}`. Single-session mode adds `interactions` and `compactions` arrays.

## session_search

Full-text search across Cowork session audit files. Searches user messages, assistant responses, tool outputs, and thinking blocks.

**Parameters:**
- **query** (required): Search string or regex pattern
- **session_id**: Optional session ID, process name fragment, or `"current"`. If omitted, searches all.
- **scope**: List of content types to search: `"user"`, `"assistant"`, `"thinking"`, `"summaries"`, `"tools"`. Default: `["user", "assistant", "summaries"]`
- **case_sensitive**: Whether search is case-sensitive (default false)
- **max_results**: Maximum total results (default 50)

**Example:**
```json
core("session_search", {"query": "archive pipeline", "scope": ["user", "assistant"], "max_results": 20})
```

**Returns:** `{query, scope, case_sensitive, sessions_searched, sessions_with_hits, total_matches, truncated, results: {<session_id>: {title, process_name, match_count, matches: [{type, excerpt, timestamp}]}}}`. Match types: `user_message`, `assistant_text`, `thinking`, `tool_use`, `tool_summary`.

## session_health

Get actionable session health summary -- error patterns, known failures, and guidance accumulated since server startup. Call after every context reset to avoid re-attempting known-broken operations.

**Parameters:**
- **brief**: If true, return only guidance lines and error rate (minimal tokens, default false)

**Returns:** `{error_rate, total_calls, total_errors, active_warnings, guidance: ["..."], known_failures: [...]}`. Brief mode returns only `guidance` and `error_rate`.

## session_recover

Recover session context from watcher signals. Cross-references audit watcher data (mentioned entities, captured sources, errors) against graph state. Returns uncommitted entities, unarchived sources, unwired sources, and error patterns with actionable guidance.

Use for cross-session handoff or after a context reset.

**Returns:** `{status, session: {process_name, session_id, tool_call_count, user_message_count, live_status, top_tools}, produced_entries, entities: {mentioned_total, in_graph, not_in_graph: [...], weakly_sourced: [...]}, sources: {captured_total, archived, unarchived: [...], unwired: [...]}, errors: {total, unique_patterns, guidance: [...]}, summary}`.

## harvest_session

Harvest analytics from session audit logs. Parses the raw audit.jsonl for a session and extracts tool usage, entity mentions, source captures, and error patterns.

**Parameters:**
- **mode**: `"enrich"` (update Neo4j CoworkSession node) or `"digest"` (return summary for create_entry). Default: `"enrich"`
- **session_id**: Session ID (e.g., `"local_89387b67-..."`)
- **process_name**: Process name (e.g., `"compassionate-ecstatic-newton"`) -- alternative to session_id

**Returns (enrich):** `{status: "enriched", session_id, process_name, stats, tool_breakdown, keywords, domains, entities_found, duration, audit_path, audit_size_mb}`

**Returns (digest):** `{status: "digest", session_id, process_name, title, model, suggested_title, suggested_domains, suggested_tags, tool_summary, key_topics, duration, user_messages, assistant_messages, total_tool_calls, entity_mentions, top_tools, audit_path, audit_size_mb}`

## backfill_discusses

Backfill DISCUSSES edges between StreamEntry and graph entities using entity name matching. Creates EntryRef proxy nodes in the corcoran database and wires DISCUSSES edges.

**Parameters:**
- **batch_size**: Max entries to process (default 50)
- **dry_run**: If true, report what would be wired without making changes (default false)
- **entry_filter**: Optional prefix filter for entry IDs (e.g. `"ls-20260228"`)

**Returns:** `{entries_processed, edges_created, entries_skipped, errors, details: [{entry_id, entity, action, ...}]}`

## dispatch_health

View call patterns, repetition warnings, and error clusters. Detects when identical calls repeat (potential loops) and surfaces error trends.

**Parameters:**
- **recent**: Number of recent calls to include (default 10, max 50)

**Returns:** `{total_calls, error_rate, active_warnings: [...], recent_calls: [{operation, timestamp, duration_ms, error}], error_clusters: [{pattern, count}]}`

## task_status

View active/recent background tasks (archive queue processing, SPN drain, captures). Tasks are auto-registered by `process_queue` and `drain_spn_queue`.

**Parameters:**
- **task_id**: Get a single task by ID
- **operation**: Filter by operation (e.g., `"process_queue"`, `"spn_drain"`)
- **status**: Filter by status (`"active"`, `"completed"`, `"failed"`, `"partial"`)
- **limit**: Max tasks to return (default 20)

**Returns:** `{tasks: [{task_id, operation, status, started_at, progress, ...}], dispatch_summary: {total_calls, active_warnings}}`

## watcher_status

Live audit watcher status and signals. Returns current watcher state: whether it's active, what session it's monitoring, accumulated entity mentions, captured sources, error signals, and top tools. No parameters required.

**Returns:** `{active, session_id, process_name, entities_mentioned, sources_captured, error_signals, top_tools, last_update}`
