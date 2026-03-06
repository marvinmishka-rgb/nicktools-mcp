> Auto-generated docs also available: `entry("help")` for the full group reference, or `entry("create_entry")` (no params) for single-operation docs.

# Workflow Operations

Lifestream entry creation, updates, and session management. These tools manage the research journal -- creating entries, wiring them to the knowledge graph, bootstrapping sessions, and auditing quality.

## Return Conventions

All workflow operations return JSON dicts with `snake_case` keys. Errors include an `"error"` key.

## Operations

- **create_entry**: Create a complete lifestream entry (.md file + Neo4j node + all edges)
- **update_entry**: Update an existing entry's Neo4j node AND .md file in one call
- **session_start**: Bootstrap a new session with full context from the lifestream graph
- **session_audit**: Run a comprehensive audit of the current session's work quality
- **phase**: Project phase lifecycle management -- create, update, transition, query, and link phases

## Routing Guide

- **New finding, idea, or decision?** -> `create_entry` (handles ID generation, .md file, Neo4j node, all edges)
- **Need to change an existing entry?** -> `update_entry` (selective field updates, add/remove edges)
- **Starting a new session?** -> `session_start` (system pulse, recent work, open questions, active phases)
- **Ending a session?** -> `session_audit` (11-check quality audit across both databases)
- **Managing project phases?** -> `phase` (create, transition, get, list, link)

## create_entry

Create a complete lifestream entry: .md file + Neo4j node + all edges. Replaces the 3-4 step manual process (query next ID, write .md, CREATE node, wire edges). APOC triggers handle inDomain, taggedWith, and followedBy automatically.

**Parameters:**
- **title** (required): Entry title
- **entry_type**: One of: `idea`, `finding`, `decision`, `question`, `connection`, `artifact`, `session`, `milestone`, `analysis`, `reflection`, `draft` (default: `"finding"`)
- **content**: Full text content for the entry
- **domains**: JSON array of domain names, e.g. `["tooling", "research"]`
  Valid domains (17): lifestream, architecture, research, tooling, corcoran, operations, neo4j, knowledge-systems, assessment, standards, methodology, spcm, development, plant-taxonomy, rhetoric, security, economics
- **tags**: JSON array of tag names, e.g. `["nicktools", "browsing"]`
- **links**: JSON object of semantic edges
  ```json
  {"connectsTo": ["ls-20260223-010"], "emergedFrom": ["ls-20260223-011"]}
  ```
- **sources**: JSON array of source objects with provenance tiers
  ```json
  [{"url": "https://...", "confidence": "archived-verified", "claim": "Kirk met Engelhardt in 2020"}]
  ```
  Confidence tiers: `archived-verified`, `web-search`, `training-knowledge`. Auto-wires CITES edges.
- **discusses**: JSON array of entity names to wire DISCUSSES edges to in the corcoran graph
  ```json
  ["Bill Montgomery", "Turning Point USA"]
  ```
  Creates EntryRef proxy node and DISCUSSES edges. Warns if entity not found.
- **status**: Entry status (default: `"active"`)
- **timeout_seconds**: Max execution time (default 30, max 60)

**Example:**
```json
{"title": "Erika Kirk Career Timeline", "entry_type": "finding", "content": "Full career analysis...", "domains": ["research", "corcoran"], "tags": ["erika-kirk", "career"], "sources": [{"url": "https://example.com/article", "confidence": "archived-verified", "claim": "Kirk joined TPUSA in 2015"}], "discusses": ["Erika Kirk", "Turning Point USA"]}
```

**Returns:** `{entry_id, title, file_path, domains, tags, sources_wired, discusses_wired, edges_created}`

## update_entry

Update an existing lifestream entry's Neo4j node AND .md file in one call. Only fields you provide will be changed -- omitted fields stay untouched. For domains/tags, the new value REPLACES the old (not appends). For links and discusses, use add/remove params.

**Parameters:**
- **entry_id** (required): The entry to update (e.g. `"ls-20260223-010"`)
- **title**: New title (or omit to keep current)
- **entry_type**: New type (or omit to keep). Must be a valid type.
- **status**: New status -- `active`, `complete`, `resolved`, `deferred`, `archived`
- **content**: New content body (or omit to keep)
- **domains**: JSON array replacing domains (or omit to keep)
- **tags**: JSON array replacing tags (or omit to keep)
- **add_links**: JSON object of edges to ADD
  ```json
  {"connectsTo": ["ls-20260223-015"]}
  ```
- **remove_links**: JSON object of edges to REMOVE
- **add_discusses**: JSON array of entity names to wire DISCUSSES edges to
- **remove_discusses**: JSON array of entity names to remove DISCUSSES edges from
- **timeout_seconds**: Max execution time (default 30, max 60)

**Returns:** `{entry_id, updated_fields: [...], file_updated}`

## session_start

Bootstrap a new session with full context from the lifestream graph. Returns: system pulse (entry/domain/tag/source counts), last 10 entries with connections and files, all open questions, domain activity, pending work items, corcoran graph summary, and suggested links.

Call this at the START of every session, before any other work.

**Parameters:**
- **focus_domain**: Optional domain to emphasize (e.g. `"corcoran"`, `"tooling"`)
- **topic**: Optional topic for full-text search (e.g. `"ghost cohort"`, `"browse_url"`)
- **timeout_seconds**: Max execution time (default 30, max 60)

**Returns:** `{pulse: {entries, domains, tags, sources}, recent_entries: [...], open_questions: [...], domain_activity: [...], corcoran_summary: {...}, suggested_links: [...]}`

## session_audit

Run a comprehensive audit of the current session's work quality. 11 checks across both databases: findings without CITES edges, failed archives, duplicate titles, orphan entries without semantic edges, null entry types (lifestream), plus graph nodes without SUPPORTED_BY edges, entries without DISCUSSES edges, weak provenance upgrade candidates, and stub entities needing detail (corcoran).

Call this at the end of every research session.

**Parameters:**
- **session_date**: Session date as `YYYY-MM-DD` (default: today)
- **timeout_seconds**: Max execution time (default 30, max 60)

**Returns:** `{issue_count, checks_run, issues: [{check, severity, details}], lifestream_checks: [...], corcoran_checks: [...]}`

## phase

Project phase lifecycle management. Phases model discrete units of work within a project -- tracking status, dependencies, linked entries, and session history. Enables context recovery across sessions without reading plan files. Each phase is a `Phase` node in the lifestream database, scoped to a project.

Phase IDs are built from `project/phase_number` (e.g. `"nicktools-1.0/4f"`). All actions require `project`; all except `list` require `phase_number`.

**Status state machine:**
- `planned` → `in_progress`, `deferred`, `blocked`
- `in_progress` → `complete`, `blocked`, `deferred`
- `blocked` → `in_progress`, `deferred`, `planned`
- `deferred` → `planned`, `in_progress`
- `complete` → `in_progress` (reopening)

**Actions:**

### phase: create

Create a new Phase node with optional parent/dependency wiring.

**Parameters:**
- **action** (required): `"create"`
- **project** (required): Project identifier (e.g. `"nicktools-1.0"`)
- **phase_number** (required): Phase number/code (e.g. `"4f"`, `"1"`, `"7a"`)
- **title**: Human-readable title (default: `"Phase {phase_number}"`)
- **description**: Scope/goals description
- **status**: Initial status (default: `"planned"`)
- **notes**: Freeform notes
- **plan_file**: Path to the plan document (stored on the node for traceability)
- **parent_phase**: Phase number of parent (e.g. `"4"` for sub-phase `"4f"`) -- wires HAS_SUBPHASE
- **depends_on**: JSON array of phase numbers this phase depends on -- wires DEPENDS_ON

**Example:**
```json
{"action": "create", "project": "nicktools-1.0", "phase_number": "4f", "title": "Lifestream graph phase tracking", "description": "Model release phases as graph nodes", "parent_phase": "4", "depends_on": ["4e"], "plan_file": "ClaudeFiles/nicktools-release-plan.md"}
```

**Returns:** `{action, phase_id, created, status, parent_phase, dependencies_wired, warnings}`

### phase: update

Update phase properties (not status -- use `transition` for that). Only provided fields are changed.

**Parameters:**
- **action** (required): `"update"`
- **project** (required): Project identifier
- **phase_number** (required): Phase number/code
- **title**: New title
- **description**: New description
- **notes**: New notes

**Returns:** `{action, phase_id, updated, title, status, properties_set}`

### phase: transition

Change phase status with enforced state machine and auto-timestamping. Sets `startedAt` on first transition to `in_progress`, `completedAt` on `complete`, `blockedAt`/`blockedReason` on `blocked`. Auto-links to current CoworkSession via WORKED_IN when transitioning to `in_progress`.

**Parameters:**
- **action** (required): `"transition"`
- **project** (required): Project identifier
- **phase_number** (required): Phase number/code
- **new_status** (required): Target status
- **reason**: Reason for blocked/deferred transitions

**Example:**
```json
{"action": "transition", "project": "nicktools-1.0", "phase_number": "4f", "new_status": "complete"}
```

**Returns:** `{action, phase_id, old_status, new_status, started_at, completed_at, session_linked}`

### phase: get

Return a phase with all its relationships: dependencies, blocks, subphases, parent, linked entries, and sessions.

**Parameters:**
- **action** (required): `"get"`
- **project** (required): Project identifier
- **phase_number** (required): Phase number/code

**Returns:** `{action, phase_id, phase: {properties...}, dependencies, blocks, subphases, parent, entries, sessions}`

### phase: list

List all phases for a project with optional status filter and summary counts.

**Parameters:**
- **action** (required): `"list"`
- **project** (required): Project identifier
- **status**: Optional status filter (e.g. `"in_progress"`, `"planned"`)

**Example:**
```json
{"action": "list", "project": "nicktools-1.0", "status": "in_progress"}
```

**Returns:** `{action, project, phases: [...], count, summary: {planned: N, in_progress: N, ...}}`

### phase: link

Wire relationships between a phase and other entities.

**Parameters:**
- **action** (required): `"link"`
- **project** (required): Project identifier
- **phase_number** (required): Phase number/code
- **link_type** (required): One of: `depends_on`, `documented_by`, `worked_in`, `has_subphase`
- **target** (required): Target identifier:
  - `depends_on`: phase number (e.g. `"4e"`)
  - `documented_by`: entry ID (e.g. `"ls-20260303-012"`)
  - `worked_in`: session processName (e.g. `"compassionate-ecstatic-newton"`)
  - `has_subphase`: phase number (e.g. `"4f"`)

**Example:**
```json
{"action": "link", "project": "nicktools-1.0", "phase_number": "4f", "link_type": "documented_by", "target": "ls-20260303-012"}
```

**Returns:** `{action, phase_id, link_type, target, linked, target_name, warnings}`
