# Architecture

*Conceptual overview of how nicktools is organized. Read this to understand the system. For file-level navigation and data flow diagrams, see [Structure Map](../STRUCTURE.md). For adapting to your domain, see [Customization Guide](CUSTOMIZATION.md).*

## Dispatch Model

nicktools exposes **5 meta-tools** that route to **55 operations** via a registry:

```
graph(operation, params)     → 10 ops  (entity CRUD, relationships, evidence, analytics)
research(operation, params)  → 18 ops  (web capture, archiving, source management)
entry(operation, params)     →  5 ops  (lifestream entries, session tracking, phase management)
core(operation, params)      → 22 ops  (scripts, files, system utilities, session health)
query(cypher, database)      → direct Neo4j Cypher passthrough
```

Each operation maps to a Python file in `tools/{group}/`. The dispatcher uses `importlib.reload()` for hot-reloading (~28ms per call). Four operations that depend on nodriver (browser automation) dispatch via subprocess instead.

Adding a new operation: create `tools/{group}/my_operation.py` with a `run(params, driver)` function, then add an entry to `TOOL_REGISTRY` in `server.py`. No MCP configuration changes needed.

## Library Layers

19 modules across 3 dependency layers with strict import rules: each layer imports only from layers below it.

```
Layer 0 — Foundation (no internal deps)
├── paths.py         Filesystem constants (all paths derive from NICKTOOLS_HOME)
├── db.py            Neo4j driver factory, execute_read/write helpers, APOC trigger management
├── io.py            stdin/stdout JSON protocol, parameter loading
├── patterns.py      PATTERNS.log auto-populating logger
├── call_monitor.py  Loop detection, error cluster analysis
├── session_detect.py  Cowork session filesystem detection
├── schema.py        Node/relationship type registry and validation
└── audit_parser.py  Streaming audit.jsonl parser

Layer 1 — Mid-tier (depends on Layer 0 only)
├── urls.py            URL canonicalization, domain extraction, source classification
├── entries.py         Lifestream entry ID generation, path normalization
├── browsing.py        Rate limiting, cache management
├── capture.py         Four-tier page capture pipeline
├── nodriver_capture.py  Subprocess entry point for anti-detection browsing
├── spn.py             Async Wayback Machine Save Page Now queue
├── task_tracker.py    Background task registry
├── audit_watcher.py   Live audit.jsonl watcher daemon thread
└── read_patterns.py   Entity read patterns (get, search, network traversal)

Layer 2 — Domain logic (depends on Layers 0-1)
├── sources.py       wire_supported_by(), wire_cites_edges() — evidence provenance
├── archives.py      Archive filesystem management, Wayback Machine API
└── write_engine.py  Batch entity writer with schema validation
```

## Four-Tier Capture Pipeline

Web page capture with automatic fallback:

```
Tier 1: HTTP + readability-lxml     (~2s, handles 80%+ of articles)
  ↓ fails (JS-rendered, empty body)
Tier 2: Chrome CDP via WebSocket    (~5s, headless browser)
  ↓ fails (bot detection, timeout)
Tier 3: Chrome CLI --dump-dom       (~10s, reliable fallback)
  ↓ fails (page dead, blocked)
Tier 4: Wayback CDX API             (~5s, archived version)
```

Smart escalation: paywall responses (401/403) skip JavaScript tiers. SPA redirects (homepage instead of article) escalate to JavaScript rendering. PDF URLs are detected and routed to a dedicated PDF extraction tool.

## Neo4j Graph Structure

Two databases, one driver:

**Knowledge graph** (default: `corcoran`) — Research entities with provenance tracking. Person, Organization, Event, Document, Property nodes linked by employment, affiliation, and evidence relationships. Every claim links to a Source node via `SUPPORTED_BY` edges. EntryRef proxy nodes link graph entities to lifestream entries via `DISCUSSES` edges.

**Lifestream** — StreamEntry nodes with YAML frontmatter mirrored as markdown files. APOC triggers auto-wire `inDomain`, `taggedWith`, and `followedBy` edges on creation. CoworkSession nodes track session metadata and chain via `PRECEDED_BY` edges. Entries link to sessions via `PRODUCED_IN`.

## Session Tracking

```
CoworkSession ←[PRODUCED_IN]— StreamEntry
     │                              │
     ├──[PRECEDED_BY]──→ CoworkSession (temporal chain)
     │                              │
     └──[COVERED_TOPIC]──→ Domain   └──[inDomain]──→ Domain
```

A daemon thread monitors the current session's audit log in real-time, extracting mentioned entities, captured sources, error signals, and tool usage patterns. This data accumulates on the CoworkSession node and powers cross-session recovery: what entities were mentioned but never committed? What sources were fetched but never archived?

## Schema Registry

`lib/schema.py` defines valid node types, relationship types, merge keys, required/optional properties, and auto-set values. The write engine validates all operations against the registry before executing Cypher. Unknown node types or invalid relationships are rejected with clear error messages.

The schema is extensible: add new node types and relationship types to the registry dictionaries. The included schema covers a real estate research domain (Person, Agent, Organization, Brokerage, Property, etc.) — adapt it for your domain. See [Customization Guide](CUSTOMIZATION.md).
