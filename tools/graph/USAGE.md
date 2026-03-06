> Auto-generated docs also available: `graph("help")` for full reference, or `graph("write")` (no params) for single-op docs.

# Graph Operations

Knowledge graph operations: create/update entities and relationships in the Corcoran investigation graph.

## Return Conventions

All graph operations return JSON dicts with `snake_case` keys. Errors include an `"error"` key. Successful writes include `status`, counts of created/updated items, and details of each operation.

## Routing Guide

- **Any entity creation** -> `graph("write", {entities: [...]})` -- handles all node types, relationships, sources, labels
- **Single entity CRUD** -> `graph("node", {action: "add", label: "Person", name: "..."})` -- schema-validated
- **Entity lookup** -> `graph("read", {entity: "..."})` -- properties + immediate relationships
- **Network exploration** -> `graph("read", {entity: "...", network: 2})` -- N-hop traversal
- **Entity search** -> `graph("read", {label: "Person", where: {...}})` -- filtered search
- **Relationship wiring** -> `graph("rel", ...)` or include in write entity's `relationships`
- **Evidence wiring** -> `graph("wire_evidence", ...)` or include in write entity's `sources`
- **Batch commits** -> `graph("commit", {operations: [...]})` -- multiple ops in one call
- **Ad-hoc Cypher** -> `graph("cypher", {query: "...", mode: "read"})` -- for APOC/complex queries
- **Graph analytics** -> `graph("gds", {action: "run", algorithm: "pageRank", ...})` -- GDS algorithms
- **Deduplication** -> `graph("deduplicate", {action: "scan"})` -- fuzzy name matching
- **Board snapshots** -> `graph("board_snapshot", {organization: "...", ...})` -- annual board membership

## write

Write entities to the graph with nested relationships, sources, and labels. The primary batch interface for graph commits during research.

**Parameters:**
- **entities** (required): List of entity dicts. Each must have `label` + merge key(s) (usually `name`).
  Optional per entity: `relationships`, `sources`, `extra_labels`, and any other keys become node properties.
- **database**: Target database (default: `"corcoran"`)

**Entity structure:**
```json
{"label": "Person", "name": "Alice Chen", "description": "Real estate advisor",
 "relationships": [
     {"type": "EMPLOYED_BY", "target": "The Agency", "target_label": "Organization",
      "props": {"role": "Senior Agent", "startDate": "2020", "current": true}}
 ],
 "sources": [{"url": "https://...", "confidence": "archived-verified", "claim": "..."}],
 "extra_labels": ["Attorney"]}
```

**Example:**
```json
graph("write", {"entities": [
    {"label": "Person", "name": "Alice Chen", "description": "Real estate advisor",
     "relationships": [{"type": "EMPLOYED_BY", "target": "The Agency", "target_label": "Organization"}],
     "sources": [{"url": "https://example.com/article", "confidence": "web-search", "claim": "Chen is an agent at The Agency"}]},
    {"label": "Organization", "name": "The Agency", "type": "Brokerage"}
]})
```

**Returns:** `{status, entities_processed, relationships_created, sources_wired, errors: [], details: [{name, label, action, relationships: [...], sources: [...]}]}`

## read

Read entities, explore networks, and search the graph. Three modes auto-detected from parameters.

**Parameters:**
- **entity**: Entity name for lookup or network center
- **network**: Hop depth for network mode (1-3). Triggers network mode.
- **label**: Node label for MATCH specificity or search filter
- **where**: Dict of search filters (`name_contains`, `name_starts`, `description_contains`)
- **include_sources**: If true, include SUPPORTED_BY sources (entity mode only, default false)
- **limit**: Max results for search mode (default 20, max 100)
- **database**: Target database (default: `"corcoran"`)

**Examples:**
```json
// Entity details + relationships
graph("read", {"entity": "Alice Chen", "label": "Person", "include_sources": true})

// N-hop network
graph("read", {"entity": "Alice Chen", "network": 2})

// Search
graph("read", {"label": "Person", "where": {"name_contains": "Chen"}, "limit": 20})
```

**Returns (entity):** `{name, label, properties: {...}, relationships: [{type, direction, target, props}], sources: [{url, confidence, claim}]}`

**Returns (network):** `{center, hops, nodes: [{name, label}], edges: [{from, to, type}], node_count, edge_count}`

**Returns (search):** `{label, filters, results: [{name, label, ...}], count}`

## node

Schema-driven node operations for any label in the registry. Individual entity CRUD.

**Parameters:**
- **action** (required): `"add"` (MERGE), `"update"` (MATCH+SET), or `"get"` (read-only)
- **label** (required): Node label (e.g. `"Person"`, `"Organization"`, `"Event"`)
- **database**: Target database (default: `"corcoran"`)
- **\*\*kwargs**: All node properties (name, description, etc.)

**Examples:**
```json
// Create a person
graph("node", {"action": "add", "label": "Person", "name": "Scott Durkin", "description": "Former Corcoran manager"})

// Update properties
graph("node", {"action": "update", "label": "Person", "name": "Scott Durkin", "description": "CEO of Douglas Elliman"})

// Look up
graph("node", {"action": "get", "label": "Person", "name": "Scott Durkin"})
```

**Returns:** `{status, action, label, name, properties: {...}}`. Get action returns full node properties.

## rel

Schema-driven relationship operations with label validation.

**Parameters:**
- **action** (required): `"add"` (MERGE), `"update"` (MATCH+SET), or `"remove"` (DELETE)
- **type** (required): Relationship type (e.g. `"EMPLOYED_BY"`, `"FAMILY_OF"`, `"AFFILIATED_WITH"`)
- **from_name** (required): Source entity name
- **to_name** (required): Target entity name
- **from_label**: Optional source label for MATCH specificity (e.g. `"Person"`)
- **to_label**: Optional target label for MATCH specificity (e.g. `"Organization"`)
- **props**: Dict of relationship properties (role, startDate, endDate, etc.)
- **database**: Target database (default: `"corcoran"`)

**Example:**
```json
graph("rel", {"action": "add", "type": "EMPLOYED_BY", "from_name": "Scott Durkin", "to_name": "Douglas Elliman", "props": {"role": "CEO", "startDate": "2022"}})
```

**Returns:** `{status, action, type, from_name, to_name}`

## wire_evidence

Wire evidence (SUPPORTED_BY) edges to Source nodes with fuzzy URL matching.

**Parameters:**
- **entity** (required): Entity name to wire evidence to
- **sources** (required): List of `{url, confidence, claim}` dicts. Each creates a SUPPORTED_BY edge.
  - `url`: Source URL (fuzzy matching resolves against existing Source nodes)
  - `confidence`: Provenance tier (`archived-verified`, `web-search`, `training-knowledge`)
  - `claim`: What this source supports (brief text)
- **label**: Optional node label for MATCH specificity
- **match_clause**: Optional custom MATCH clause for complex merge keys (must use `n` as node alias)
- **extra_params**: Optional dict of extra Cypher params for custom match_clause
- **database**: Target database (default: `"corcoran"`)

**Example:**
```json
graph("wire_evidence", {"entity": "Gabriel Minsky", "sources": [
    {"url": "https://therealdeal.com/minsky-article", "confidence": "archived-verified", "claim": "Minsky was VP of Development"}
]})
```

**Returns:** `{status, entity, sources_wired, details: [{url, matched_source, edge_created}]}`

## commit

Execute a batch of node, rel, and wire_evidence operations sequentially. Preferred over individual calls for research graph commits.

**Parameters:**
- **operations** (required): List of operation dicts. Each must have an `op` key:
  - `{op: "node", action: "add", label: "Person", name: "...", ...}`
  - `{op: "rel", action: "add", type: "EMPLOYED_BY", from_name: "...", to_name: "...", ...}`
  - `{op: "wire_evidence", entity: "...", sources: [...], ...}`
- **continue_on_error**: If true, attempt all operations even if some fail (default false)
- **database**: Default database for all operations (individual ops can override, default: `"corcoran"`)

**Example:**
```json
graph("commit", {"operations": [
    {"op": "node", "action": "add", "label": "Person", "name": "Jane Doe", "description": "Attorney"},
    {"op": "rel", "action": "add", "type": "AFFILIATED_WITH", "from_name": "Jane Doe", "to_name": "TPUSA"},
    {"op": "wire_evidence", "entity": "Jane Doe", "sources": [{"url": "https://...", "confidence": "web-search", "claim": "..."}]}
]})
```

**Returns:** `{status, operations_total, operations_succeeded, operations_failed, results: [{op, status, ...}]}`

## cypher

Execute arbitrary Cypher with EXPLAIN-based read safety validation. Supports APOC, GDS, and standard Cypher.

**Parameters:**
- **query** (required): Cypher query string
- **mode**: Execution mode (default: `"auto"`):
  - `"read"`: EXPLAIN-validated read-only. Rejects write queries.
  - `"write"`: Executes any query without safety check.
  - `"auto"`: Classifies via EXPLAIN, routes accordingly.
- **database**: Target database (default: `"corcoran"`)
- **params**: Optional dict of Cypher parameters
- **max_records**: Maximum records to return (default 1000, 0 for unlimited)

**Example:**
```json
graph("cypher", {"query": "MATCH (p:Person)-[r:EMPLOYED_BY]->(o:Organization) RETURN p.name, type(r), o.name LIMIT 10", "mode": "read"})
```

**Returns:** `{mode, records: [{...}], count, database}`

## gds

Run GDS (Graph Data Science) algorithms with managed projection lifecycle. Projections are auto-created and cleaned up.

**Parameters:**
- **action** (required): `"run"`, `"list"`, or `"estimate"`
- **algorithm**: Algorithm short name (e.g. `"pageRank"`, `"louvain"`, `"betweenness"`). Use `action="list"` to see options.
- **nodes**: Node labels to include in projection. String or list.
- **relationships**: Relationship types for projection. String or list.
- **config**: Optional dict of algorithm-specific configuration (e.g. `{"maxIterations": 20}`)
- **projection_name**: Optional custom name (auto-generated if omitted)
- **database**: Target database (default: `"corcoran"`)
- **max_records**: Maximum results to return (default 500)

**Example:**
```json
graph("gds", {"action": "run", "algorithm": "pageRank", "nodes": ["Person", "Organization"], "relationships": ["EMPLOYED_BY", "AFFILIATED_WITH"]})
```

## board_snapshot (optional)

*Domain extension: organizational research -- tracks board membership over time. Safe to remove if not needed.*

Load board members for an organization, create Person nodes and AFFILIATED_WITH edges, detect year-over-year changes.

**Parameters:**
- **organization** (required): Organization name (must exist in graph)
- **fiscal_year** (required): Fiscal year string (e.g. `"2024"`)
- **members** (required): List of member dicts: `[{name, role, compensation?}]`
- **source_url**: URL of the source document (e.g. 990 filing)
- **source**: Text description of the source
- **database**: Target database (default: `"corcoran"`)

**Example:**
```json
graph("board_snapshot", {"organization": "TPUSA", "fiscal_year": "2024", "members": [
    {"name": "Charlie Kirk", "role": "President", "compensation": 400000},
    {"name": "Tyler Bowyer", "role": "COO", "compensation": 320000}
], "source_url": "https://projects.propublica.org/nonprofits/organizations/462672267"})
```

## deduplicate

Find and merge duplicate entities using fuzzy name matching and APOC mergeNodes.

**Parameters:**
- **action** (required): `"scan"` (find candidates) or `"merge"` (combine two entities)
- **label**: For scan: node label to scan (e.g. `"Person"`). Scans all if omitted.
- **threshold**: For scan: minimum similarity score (default 0.7)
- **keep**: For merge: name of the node to keep
- **remove**: For merge: name of the node to merge into keep and delete
- **merge_properties**: For merge: copy properties from remove to keep (default true)
- **database**: Target database (default: `"corcoran"`)

**Examples:**
```json
// Scan for duplicates
graph("deduplicate", {"action": "scan", "label": "Person", "threshold": 0.8})

// Merge two confirmed duplicates
graph("deduplicate", {"action": "merge", "keep": "Charlie Kirk", "remove": "Charles Kirk"})
```
