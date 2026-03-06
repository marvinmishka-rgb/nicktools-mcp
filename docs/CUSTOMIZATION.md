# Customization Guide

nicktools ships with a real estate research domain (the Corcoran Group) as a working example. Here's how to adapt it for your own research.

## Environment Setup

Copy `.env.example` to `.env` and set your values:

```bash
cp .env.example .env
```

The key settings are `NEO4J_PASSWORD` (required) and `NICKTOOLS_HOME` (defaults to `~/nicktools_workspace`). All data directories — archives, lifestream, queue — are created under `NICKTOOLS_HOME/ClaudeFiles/` automatically.

## Database Configuration

nicktools uses two Neo4j databases: one for your knowledge graph (default name: `corcoran`) and one for the lifestream (`lifestream`). To change the knowledge graph database name, update the `database` parameter defaults throughout the codebase, or pass `database="your_name"` explicitly in tool calls.

The lifestream database stores analytical entries and session metadata. Its name (`lifestream`) is referenced in APOC trigger definitions (`lib/db.py`) and entry creation tools — keep it as-is unless you're restructuring the session system.

## Schema: Adding Your Domain's Entity Types

The schema registry in `lib/schema.py` defines what node types and relationships your graph supports. The included types cover real estate research (Person, Agent, Organization, Brokerage, etc.) — extend or replace them for your domain.

### Adding a Node Type

Add an entry to `NODE_TYPES` in `lib/schema.py`:

```python
"Patent": {
    "merge_key": "patentNumber",      # Property used in MERGE clause
    "required": ["patentNumber"],      # Must be provided
    "optional": ["title", "filingDate", "inventors", "assignee"],
    "auto_set": {"addedDate": "date()"},  # Set automatically on CREATE
    "extra_labels": False,             # Allow dynamic labels?
    "extra_props": True,               # Allow unlisted properties?
},
```

### Adding a Relationship Type

Add an entry to `REL_TYPES` in `lib/schema.py`:

```python
"INVENTED_BY": {
    "from": ["Patent"],
    "to": ["Person"],
    "props": ["role", "source"],
},
```

The write engine validates all operations against these registries. Invalid types are rejected with clear error messages.

## Source Classification

`lib/urls.py` contains `SOURCE_TYPE_MAP` — a mapping from domain names to reliability categories. The included categories reflect source reliability tiers used in investigative research:

- `primary-journalism` — editorial standards, fact-checking (NYT, WaPo, etc.)
- `encyclopedic` — editorial review, sourced claims (Wikipedia, Britannica)
- `public-record` — government sources, official filings (SEC, courts)
- `advocacy-research` — mission-driven but researched organizations
- `tabloid` — entertainment framing, light sourcing
- `industry-source` — trade publications, company sites
- `blog-factual-substrate` — individual researchers with track records

Edit the map to add your domain's important sources. The `sourceType` property is auto-set on Source nodes during archiving.

## Session Keywords

`lib/audit_parser.py` contains `DOMAIN_KEYWORDS` — word sets that help the audit watcher detect which research domain a session is working in. Update these for your domains:

```python
DOMAIN_KEYWORDS = {
    "biotech": {"gene", "therapy", "clinical", "trial", "fda", "patent"},
    "finance": {"sec", "filing", "disclosure", "fund", "portfolio"},
    "tooling": {"tool", "nicktools", "mcp", "server", "dispatch", "lib", "script"},
}
```

## Adding New Tool Operations

1. Create a Python file in `tools/{group}/` (e.g., `tools/research/my_search.py`)
2. Implement `def run(params, driver=None)` that returns a result dict
3. Add the operation to `TOOL_REGISTRY` in `server.py`:

```python
"my_search": {"script": "research/my_search.py", "impl": ("research.my_search", "_impl"), "timeout": 30},
```

The dispatcher handles parameter parsing, timeout management, and error formatting. Your tool just needs to do its work and return a dict.

## The Corcoran Example

The `examples/corcoran/` directory contains a full knowledge graph export from real research. Use it as a reference for how nicktools structures data: evidence chains (`SUPPORTED_BY`), entity resolution (`RESOLVES_TO`), geographic taxonomy (`IN_MARKET`, `IN_REGION`, `WORKED_IN`), and temporal tracking (date ranges on employment edges).

Load it into a fresh Neo4j database to explore the structure before building your own.
