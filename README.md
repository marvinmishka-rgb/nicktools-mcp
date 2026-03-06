# nicktools alpha release

A knowledge graph MCP server for research with source provenance. 55 operations across 5 meta-tools, a four-tier web capture pipeline, and a self-documenting session system — all backed by Neo4j.

Built over 15 days through 41 human-AI collaboration sessions. Includes a proof-of-concept knowledge graph (5,276 nodes, 9,699 relationships) from real investigative research.

## Why This Exists

Most memory and research tools for LLMs store text in flat files or vector databases. Claims accumulate without attribution. Sources are read but never archived. Context is lost between sessions. When you need to verify something you wrote last week, you're back to searching.

nicktools takes a different approach: **the knowledge graph is the primary research output.** Every entity links to sources via evidence edges. Every source is archived with provenance tracking. Every session is recoverable from the graph. When Claude's context window resets, the system remembers what already failed and what was already found.

## What You Get

```
graph(operation, params)     → 10 ops  Entity CRUD, relationships, evidence, graph analytics
research(operation, params)  → 18 ops  Web capture, archiving, source management
entry(operation, params)     →  5 ops  Lifestream entries, session tracking, phase management
core(operation, params)      → 22 ops  Scripts, files, system utilities, session health
query(cypher, database)      →         Direct Neo4j Cypher with EXPLAIN-based safety
```

**Key capabilities:**

- **Four-tier web capture**: HTTP + readability → Chrome CDP → Chrome CLI → Wayback Machine. Automatic fallback, paywall detection, SPA escalation.
- **Source provenance**: Every claim traces to a Source node via `SUPPORTED_BY` edges. Sources are archived locally and submitted to the Wayback Machine.
- **Session intelligence**: A daemon thread monitors the current session, extracting entity mentions, captured sources, and error patterns. Cross-session recovery tells you what was found but never committed.
- **Self-documenting**: Reference docs auto-generate from live system state. Tool inventory, database schema, research standards — always current.
- **Schema-validated writes**: A registry of node types, relationship types, and merge keys. The write engine validates everything before it touches the database.

## Quick Start

### Prerequisites

- **Python 3.13+** — [python.org](https://python.org)
- **Neo4j 5.x or 2024+** — Desktop or Server. Enterprise Edition required for multiple databases; Community Edition works but limits you to one user database plus system.
- **APOC plugin** — Required for automatic edge wiring (triggers). Install via Neo4j Desktop's plugin manager or download from [neo4j-contrib/neo4j-apoc-procedures](https://github.com/neo4j-contrib/neo4j-apoc-procedures).
- **GDS plugin** (optional) — For graph analytics (PageRank, community detection). Not required for core functionality.
- **An MCP-compatible client** — Claude Desktop (Cowork mode), Claude Code, or any MCP client that supports stdio transport.

### Installation

1. **Clone and configure**:
   ```bash
   git clone https://github.com/yourusername/nicktools.git
   cd nicktools
   cp .env.example .env
   ```

2. **Edit `.env`** — the only required setting is your Neo4j password:
   ```
   NEO4J_PASSWORD=your_password_here
   ```
   See `.env.example` for optional settings: workspace path, Wayback Machine credentials, public records API keys.

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Create your Neo4j databases**:

   In the Neo4j Browser or `cypher-shell`, create two databases:
   ```cypher
   CREATE DATABASE nicktools IF NOT EXISTS;
   CREATE DATABASE lifestream IF NOT EXISTS;
   ```
   The knowledge graph database name defaults to `corcoran` — override it in `.env` with `NICKTOOLS_GRAPH_DB=your_name`. The lifestream database name is `lifestream` by default (override with `NICKTOOLS_ENTRY_DB`).

   **APOC triggers**: The server auto-configures APOC triggers on first startup. These create domain, tag, and temporal chain edges automatically when you create lifestream entries. No manual trigger setup needed — just make sure APOC is installed.

5. **Windows users** — set `PYTHONUTF8=1` as a system environment variable before running the server. Python defaults to cp1252 on Windows, which crashes on non-ASCII output:
   ```powershell
   [System.Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "User")
   ```
   Restart your terminal after setting this. Not needed on Linux/macOS.

6. **Verify the installation**:
   ```bash
   python server.py --test
   ```
   This checks Neo4j connectivity, database existence, APOC availability, and basic tool dispatch. On a fresh install with empty databases, all 16 core operations should pass.

7. **Add to your MCP client**:

   For Claude Desktop, add to your MCP configuration:
   ```json
   {
     "mcpServers": {
       "nicktools": {
         "command": "python",
         "args": ["path/to/nicktools/server.py"]
       }
     }
   }
   ```

   The server starts in stdio mode and exposes all 5 meta-tools. Call any tool with `operation="help"` to get full documentation.

## Architecture

```
┌──────────────────────────────────────────┐
│           5 Meta-Tools (MCP)             │
│  graph · research · entry · core · query │
└───────────────┬──────────────────────────┘
                │ TOOL_REGISTRY dispatch
┌───────────────▼──────────────────────────┐
│         55 Tool Operations               │
│  tools/graph/ · research/ · workflow/ ·  │
│  core/                                   │
└───────────────┬──────────────────────────┘
                │ importlib.reload (~28ms)
┌───────────────▼──────────────────────────┐
│         Library Layers (lib/)            │
│  L0: paths, db, io, schema, patterns    │
│  L1: urls, entries, capture, browsing   │
│  L2: sources, archives, write_engine    │
└───────────────┬──────────────────────────┘
                │
┌───────────────▼──────────────────────────┐
│            Neo4j Databases               │
│  Knowledge Graph + Lifestream            │
└──────────────────────────────────────────┘
```

Adding a new tool operation costs zero MCP configuration changes — drop a Python file in `tools/{group}/`, add one line to the registry, and the dispatcher handles the rest. Tool modules reload on every call (~28ms), so edits take effect immediately without server restart.

For details: [Architecture](docs/ARCHITECTURE.md) · [Structure Map](STRUCTURE.md)

## Known Limitations

**Developed and tested on Windows only.** The server and library code are pure Python and should work on Linux/macOS, but the browsing pipeline (Chrome CDP, nodriver) and some path handling have only been tested on Windows. The `run_command` operation defaults to PowerShell. Cross-platform testing has not been done.

**No automated test suite.** The `--test` flag runs a basic connectivity check, not a comprehensive test suite. Testing has been manual across 41 development sessions. Individual operations are well-exercised through real use, but there are no unit tests or CI.

**Session intelligence requires Cowork mode.** The audit watcher, session detection, and context recovery features depend on Cowork's `audit.jsonl` files and session filesystem layout. Without Cowork, the core tools (graph, research, entry, query) work normally, but `session_health`, `session_recover`, and `watcher_status` are unavailable.

**Browsing tiers 2-3 require Chrome installed.** The four-tier capture pipeline falls back to Chrome CDP and Chrome CLI when HTTP + readability fails. If Chrome isn't installed, these tiers are skipped and the pipeline falls through to Wayback CDX (Tier 4) for JS-rendered pages.

**VPN can interfere with captures.** VPN hooks can intercept Chrome's loopback connections and block Wayback Machine submissions. If you use a VPN, consider split tunneling for Python.

**Neo4j Enterprise required for multi-database.** Community Edition supports only one user database plus the system database. If using Community Edition, you'll need to put the knowledge graph and lifestream in the same database (which requires schema changes to avoid label collisions).

**No Docker image.** Deployment is manual (clone + pip install + Neo4j). A containerized deployment is not yet available.

**Skills are not packaged.** The research-draft, research-verify, graph-commit, and system-dev skills are designed for the development environment and reference internal paths. Adapting them for a new installation requires editing the skill files.

**The proof-of-concept graph is domain-specific.** The included Corcoran example demonstrates the data model, but adapting nicktools to a new research domain requires modifying `lib/schema.py` (entity types), `lib/urls.py` (source classification), and `lib/audit_parser.py` (domain keywords). See the [Customization Guide](docs/CUSTOMIZATION.md).

## Proof of Concept

The `examples/corcoran/` directory contains a knowledge graph exported from real investigative research into the Corcoran Group real estate brokerage. 5,276 nodes across 14 types (Person, Agent, Organization, Event, Property, Source, and more) with 9,699 relationships. PII has been redacted.

Load it into Neo4j to explore how nicktools structures research data: evidence chains, entity resolution, geographic taxonomy, and temporal tracking.

## Documentation

- **[Development Story](docs/DEVELOPMENT_STORY.md)** — How nicktools was built: 15 days of human-AI architecture across 41 sessions, from Python stdout bugs to a unified graph interface. Includes the seven most interesting technical failures and what they taught us.
- **[Architecture](docs/ARCHITECTURE.md)** — Dispatch model, library layers, capture pipeline, graph structure, session tracking.
- **[Structure Map](STRUCTURE.md)** — Directory layout, library layer rules, data flow diagrams, dispatch model. Read before modifying any tool.
- **[Customization Guide](docs/CUSTOMIZATION.md)** — Adapt nicktools for your research domain: schema types, source classification, session keywords, adding new tools.
- **Tool reference** — Each tool group has a USAGE.md in its directory (`tools/graph/USAGE.md`, `tools/research/USAGE.md`, etc.) with routing guides and parameter documentation. Or call any tool with `operation="help"` for inline docs.

## Dependencies

- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework
- [Neo4j Python Driver](https://neo4j.com/docs/python-manual/current/) — Graph database access
- [nodriver](https://github.com/ultrafunkamsterdam/nodriver) — Anti-detection browser automation (Tier 2 capture)
- [readability-lxml](https://github.com/buriy/python-readability) — Article text extraction
- [PyMuPDF](https://pymupdf.readthedocs.io/) — PDF text extraction
- [requests](https://docs.python-requests.org/) — HTTP client
- [PyYAML](https://pyyaml.org/) — Configuration parsing
- [python-dotenv](https://github.com/theskumar/python-dotenv) — Environment file loading

See `requirements.txt` for pinned versions.

## License

MIT
