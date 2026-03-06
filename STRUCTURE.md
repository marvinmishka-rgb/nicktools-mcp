# nicktools MCP v3.0.0 — Structure Map

*File-level reference for navigating and modifying the codebase. Read this before editing any tool or library module. For a conceptual overview, see [Architecture](docs/ARCHITECTURE.md). For tool parameter documentation, see the USAGE.md in each tool group directory.*

## Directory Layout

```
nicktools_mcp/
├── server.py              # FastMCP entry point, TOOL_REGISTRY, meta-tool dispatch
├── STRUCTURE.md           # This file
├── lib/                   # Shared library (3 layers, 19 modules)
│   ├── paths.py           # Layer 0 — filesystem constants (CLAUDE_FILES, ARCHIVES_DIR, etc.)
│   ├── db.py              # Layer 0 — Neo4j driver factory (get_neo4j_driver, URI/creds)
│   ├── io.py              # Layer 0 — stdin param loading, stdout JSON output, encoding
│   ├── patterns.py        # Layer 0 — tool usage pattern logger (auto-appends to PATTERNS.log)
│   ├── call_monitor.py    # Layer 0 — dispatch call monitor (loop detection, error clusters, cumulative patterns)
│   ├── session_detect.py  # Layer 0 — Cowork session detection from filesystem (cached at startup)
│   ├── schema.py          # Layer 0 — schema validation for graph operations
│   ├── audit_parser.py    # Layer 0 — streaming audit.jsonl parser (keywords, tool counts, domain signals)
│   ├── urls.py            # Layer 1 — canonicalize_url, extract_domain, fuzzy_match_source
│   ├── entries.py         # Layer 1 — entry ID generation, path normalization, markdown templates
│   ├── browsing.py        # Layer 1 — rate limiting, cache management, nodriver config
│   ├── capture.py         # Layer 1 — four-tier page capture (HTTP → nodriver → Chrome CLI → Wayback)
│   ├── nodriver_capture.py # Layer 1 — subprocess entry point for nodriver tier (temp file IPC)
│   ├── spn.py             # Layer 1 — async SPN queue (enqueue_spn, drain_spn_queue, spn_queue_status)
│   ├── task_tracker.py    # Layer 1 — background task registry (register, update, query, JSONL persist)
│   ├── audit_watcher.py   # Layer 1 — live audit.jsonl watcher (daemon thread, session metadata, signal extraction)
│   ├── write_engine.py    # Layer 2 — batch entity writer (nodes + rels + evidence in one call)
│   ├── read_patterns.py   # Layer 1 — entity/network read patterns (get, search, neighborhood)
│   ├── sources.py         # Layer 2 — wire_supported_by, wire_cites_edges (ALL Source-edge wiring)
│   └── archives.py        # Layer 2 — archive_paths, discover_archives, reconcile_with_graph
├── tools/
│   ├── graph/             # Entity creation & relationship wiring (10 operations)
│   │   ├── USAGE.md       # Documentation for graph operations
│   │   ├── PATTERNS.log   # Auto-populated tool usage log (grep-friendly)
│   │   ├── write_ops.py         # High-level batch write (entities + rels + sources)
│   │   ├── read_ops.py          # High-level entity read (get, search, neighborhood)
│   │   ├── node_ops.py          # Generic node CRUD (add/update/get)
│   │   ├── rel_ops.py           # Generic relationship CRUD
│   │   ├── wire_evidence.py     # SUPPORTED_BY edge wiring
│   │   ├── commit_ops.py        # Batch graph commits (nodes + rels + evidence)
│   │   ├── cypher_passthrough.py # Ad-hoc Cypher with EXPLAIN-based safety
│   │   ├── gds_ops.py           # Graph Data Science algorithms
│   │   ├── board_snapshot.py    # Board/executive membership snapshots
│   │   ├── dedup_ops.py         # Entity deduplication
│   │   ├── add_person.py        # (removed from registry — kept as reference)
│   │   ├── add_organization.py  # (removed from registry — kept as reference)
│   │   ├── add_event.py         # (removed from registry — kept as reference)
│   │   ├── add_document.py      # (removed from registry — kept as reference)
│   │   ├── add_property.py      # (removed from registry — kept as reference)
│   │   ├── connect_entities.py  # (removed from registry — kept as reference)
│   │   └── graph_network.py     # (removed from registry — kept as reference)
│   ├── research/          # Web research, archiving, source management (18 operations)
│   │   ├── USAGE.md
│   │   ├── PATTERNS.log
│   │   ├── browse_url.py        [subprocess — nodriver]
│   │   ├── archive_source.py    [subprocess — nodriver]
│   │   ├── save_page.py         [subprocess — nodriver]
│   │   ├── check_sources.py     [subprocess — nodriver]
│   │   ├── extract_saved_article.py
│   │   ├── search_pdf.py
│   │   ├── wayback_lookup.py
│   │   ├── archive_inventory.py
│   │   ├── search_records.py
│   │   ├── queue_archive.py     # Submit URLs to async capture queue
│   │   ├── check_queue.py       # Check capture queue status
│   │   ├── read_staged.py       # Read staged capture results
│   │   ├── process_queue.py     # Four-tier capture queue processor
│   │   ├── ingest_saved.py      # Manual capture ingestion from uploads/websites/
│   │   └── generate_report.py   # Research report generation
│   ├── workflow/          # Lifestream entries & session management (5 operations)
│   │   ├── USAGE.md
│   │   ├── PATTERNS.log
│   │   ├── create_entry.py
│   │   ├── update_entry.py
│   │   ├── session_start.py
│   │   ├── session_audit.py
│   │   └── phase_ops.py        # Phase lifecycle: create, update, transition, get, list, link
│   └── core/              # System utilities & direct execution (22 operations)
│       ├── USAGE.md
│       ├── PATTERNS.log
│       ├── run_script.py
│       ├── run_python.py
│       ├── run_command.py
│       ├── list_scripts.py
│       ├── read_file.py
│       ├── write_file.py
│       ├── read_document.py
│       ├── neo4j_query.py
│       ├── server_info.py
│       ├── restart_server.py
│       ├── registry_sync.py
│       ├── sync_system_docs.py    # Auto-regenerate reference docs
│       ├── backup_graph.py        # Neo4j database backup
│       ├── session_ingest.py      # CoworkSession node backfill and wiring
│       ├── harvest_session.py     # Audit analytics: keywords, tool counts, domain signals → Neo4j
│       ├── session_costs.py       # Token cost analysis from audit.jsonl
│       ├── session_search.py      # Full-text search across audit logs
│       ├── dispatch_health.py     # Call pattern diagnostics (loop detection, error clusters)
│       ├── task_status.py         # Background task visibility (process_queue, SPN drain, etc.)
│       ├── session_health.py      # Cross-context-window intelligence (error patterns, guidance)
│       ├── backfill_discusses.py  # Wire DISCUSSES edges for entries missing them
│       ├── harvest_session.py     # Audit analytics: keywords, tool counts → Neo4j
│       ├── watcher_status.py      # Live audit watcher state and signal summary
│       └── session_recover.py     # Cross-reference watcher data against graph for handoff
```

## Library Layer Rules

Strict import hierarchy — each layer may only import from layers below it.

```
Layer 2: sources.py, archives.py, write_engine.py
    ↓ imports from
Layer 1: urls.py, entries.py, browsing.py, capture.py, read_patterns.py, spn.py,
         task_tracker.py, audit_watcher.py
    ↓ imports from
Layer 0: paths.py, db.py, io.py, patterns.py, call_monitor.py, session_detect.py,
         schema.py, audit_parser.py
```

**Key modules to know:**

| Module | Critical Functions | Used By |
|--------|-------------------|---------|
| `urls.py` | `canonicalize_url()` | ALL tools creating/matching Source nodes |
| `urls.py` | `fuzzy_match_source()` | `sources.py` (CITES wiring) |
| `sources.py` | `wire_supported_by()` | `add_person`, `add_organization`, `add_event`, `add_document`, `add_property` |
| `sources.py` | `wire_cites_edges()` | `create_entry` |
| `archives.py` | `archive_paths()` | `archive_source`, `extract_saved_article` |
| `archives.py` | `discover_archives()` | `archive_inventory`, `session_audit` |
| `db.py` | `get_neo4j_driver()` | Every tool that touches Neo4j |
| `entries.py` | `generate_entry_id()` | `create_entry` |
| `capture.py` | `capture_page()` | `process_queue` (three-tier: HTTP→Chrome→Wayback) |
| `capture.py` | `save_capture()` | `process_queue` (filesystem archive write) |
| `spn.py` | `enqueue_spn()` | `queue_archive`, `process_queue`, `archive_source` (fire-and-forget) |
| `spn.py` | `drain_spn_queue()` | `spn_worker.py` (standalone background worker) |
| `browsing.py` | `enforce_rate_limit()` | `browse_url`, `archive_source`, `process_queue` |
| `patterns.py` | `log_pattern()` | server.py `_dispatch()` (auto-logging after every tool call) |
| `call_monitor.py` | `record_call()`, `get_dispatch_warning()` | server.py `_dispatch()` (loop detection, error clusters) |
| `call_monitor.py` | `get_stats()`, `check_repetition()` | `dispatch_health` tool (call pattern diagnostics) |
| `call_monitor.py` | `get_error_patterns()`, `get_session_summary()` | `session_health` tool (cross-context-window intelligence) |
| `session_detect.py` | `detect_current_session()` | server.py startup (session auto-detection, stderr logging) |
| `session_detect.py` | `get_cached_session()` | `create_entry` (PRODUCED_IN linking without timestamp window) |
| `task_tracker.py` | `register_task()`, `update_task()` | `process_queue`, `spn.drain_spn_queue` (background task visibility) |
| `task_tracker.py` | `get_tasks()`, `get_task()` | `task_status` tool (query interface for Claude) |
| `audit_watcher.py` | `start_watcher()`, `stop_watcher()` | server.py startup/shutdown (live session monitoring) |
| `audit_watcher.py` | `get_watcher_status()` | `watcher_status` tool (watcher state + signal summary) |

## PATTERNS.log — Tool Usage Logs

Each tool group has a `PATTERNS.log` file that auto-populates after every tool call. The dispatcher in `server.py` calls `lib/patterns.py:log_pattern()` after each dispatch.

**Format** (tab-separated, one line per call):
```
TIMESTAMP	OPERATION	STATUS	DURATION	SIGNAL
2026-02-26 16:53:07	graph_network	OK	114ms	entity=Erika Kirk | entities=28 | edges=28
2026-02-26 16:51:10	registry_sync	OK	98ms	status=ok | ops=31
```

**Smart filtering**: The SIGNAL field extracts key data per operation type — entity names, URLs archived, capture status, entry IDs, cypher queries, error messages — instead of dumping raw results.

**Grep examples**:
```bash
# Find all graph_network calls for a specific entity
grep "graph_network" tools/graph/PATTERNS.log | grep "Kirk"

# Find all errors across all groups
grep "ERROR" tools/*/PATTERNS.log

# Find all archive_source calls with their capture status
grep "archive_source" tools/research/PATTERNS.log

# Find slow calls (>1000ms)
grep -P "\t[1-9]\d{3,}ms\t" tools/*/PATTERNS.log
```

**Skipped operations**: `server_info`, `list_scripts`, and `help` calls are not logged (too noisy, low value).

## Dispatch Model

```
Claude calls:  graph("add_person", '{"name": "..."}')
                 │
server.py        ├─ lookup TOOL_REGISTRY["graph"]["operations"]["add_person"]
                 ├─ run preprocess function (_preprocess_graph_tool)
                 ├─ check IN_PROCESS_TOOLS vs SUBPROCESS_ONLY
                 │
In-process:      ├─ importlib.reload(tools.graph.add_person)
(26 tools)       └─ call add_person_impl(name=..., driver=shared_driver)
                      ~28ms round-trip
                 │
Subprocess:      ├─ subprocess.run(["python", "tools/research/archive_source.py"])
(4 tools)        └─ params via stdin JSON, result via stdout JSON
                      ~2-5s round-trip (browser startup)
```

**Subprocess-only tools** (require their own event loop for nodriver):
`browse_url`, `archive_source`, `save_page`, `check_sources`

## Data Flow: Source Nodes

This is the most important data flow to understand — it's where bugs hide.

```
URL enters system
    │
    ├─ process_queue ──── canonicalize_url() ──→ MERGE (s:Source {url: $canonical})  [preferred path]
    ├─ archive_source ─── canonicalize_url() ──→ MERGE (s:Source {url: $canonical})
    ├─ save_page ──────── canonicalize_url() ──→ MERGE (s:Source {url: $canonical})
    ├─ extract_saved_article ── canonicalize_url() ──→ MERGE (s:Source {url: $canonical})
    ├─ search_pdf ─────── canonicalize_url() ──→ MERGE (s:Source {url: $canonical})
    │
    ├─ wire_supported_by ─ canonicalize_url() → fuzzy_match → MERGE Source + SUPPORTED_BY edge
    ├─ wire_cites_edges ── canonicalize_url() → fuzzy_match → MERGE Source + CITES edge
    │
    └─ connect_entities ── canonicalize_url() on sourceUrl property (stored on edge, not Source node)

    ⚠ RULE: Never MERGE Source nodes with raw URLs. Always canonicalize first.
    ⚠ RULE: Never write raw Cypher to create Source nodes — use the tools above.
```

## Data Flow: Entity Creation (graph tools)

```
graph("add_person", params)
    │
    add_person_impl()
    ├── Create/merge Person node
    ├── Wire employment (EMPLOYED_BY/WORKED_AT → Organization)
    ├── Wire affiliations (AFFILIATED_WITH → Organization)
    ├── Wire family (FAMILY_OF → Person)
    ├── Wire SUPPORTED_BY → Source nodes (via lib/sources.py wire_supported_by)
    └── Return summary with warnings
```

All graph tools (`add_person`, `add_organization`, `add_event`, `add_document`, `add_property`)
follow this same pattern and delegate source-wiring to `lib/sources.py`.

## Data Flow: Lifestream Entries

```
entry("create_entry", params)
    │
    create_entry_impl()
    ├── Generate ID (ls-YYYYMMDD-NNN)
    ├── Create StreamEntry node (lifestream DB)
    ├── Write markdown file to lifestream/stream/YYYY/MM/DD/
    ├── Wire CITES → Source nodes (via lib/sources.py wire_cites_edges)
    ├── Wire DISCUSSES → Person/Org/Event nodes (cross-DB: creates EntryRef in corcoran)
    └── APOC triggers auto-create: inDomain, taggedWith, followedBy edges
```

## Neo4j Database Routing

| Database | What lives there | Primary tools |
|----------|-----------------|---------------|
| corcoran | Person, Organization, Event, Document, Property, Source, Agent, EntryRef | All `graph()` tools, `archive_source`, `save_page`, `extract_saved_article` |
| lifestream | StreamEntry, Source, Domain, Tag, File | `create_entry`, `update_entry`, `session_start`, `session_audit` |
| system | DB metadata | `neo4j_query` with `database="system"` |

**Cross-database pattern**: `create_entry` writes to lifestream (StreamEntry) AND corcoran (EntryRef proxy node + DISCUSSES edges).

## Operational Rules

1. **URL Canonicalization**: Every code path that creates or matches a Source node MUST use `canonicalize_url()` from `lib/urls.py`. This prevents www./trailing-slash duplicates. Resolved 2026-02-26 after finding 12 duplicate pairs.

2. **Source Edge Wiring**: Always use `lib/sources.py` functions (`wire_supported_by`, `wire_cites_edges`). Never write raw Cypher for SUPPORTED_BY or CITES edges — the library handles canonicalization, fuzzy matching, and provenance validation.

3. **Rate Limiting**: `browse_url` and `archive_source` share a rate-limit file via `lib/browsing.py`. The default delay between requests to the same domain is configurable.

4. **Adding a New Tool**:
   - Create `tools/{group}/{operation}.py` with `{operation}_impl()` function
   - Add to TOOL_REGISTRY in `server.py`
   - Add to the group's USAGE.md
   - If it creates Source nodes, import and use `canonicalize_url()`
   - If it wires sources, use `lib/sources.py`
   - Cost: 0 additional context tokens (USAGE.md loaded on demand)

5. **Testing**: `python server.py --test` runs basic self-test. For individual tools: call with `operation="help"` to verify USAGE.md loads, then test with params.
