# Corcoran Knowledge Graph — Proof of Concept

A knowledge graph built over 41 human-AI collaboration sessions (February 15 -- March 3, 2026), investigating the Corcoran Group real estate brokerage and its surrounding ecosystem of people, organizations, and events.

This export demonstrates what nicktools produces: a structured, source-backed research graph where every claim is traceable to archived evidence. It is real investigative research, not a toy example.

## The Research Domain

The Corcoran Group is a luxury real estate brokerage founded in New York City, now operating across 14 markets nationwide. The research maps the organization's internal structure (agents, offices, teams, markets), key personnel and their career trajectories, corporate events (office openings, leadership changes, lawsuits), and the broader network of affiliated organizations and individuals.

The graph extends beyond Corcoran itself into adjacent domains where the research led: political organizations connected through shared personnel, legal proceedings involving brokerage operations, real estate development firms, and media entities. This is typical of investigative research -- you start with one subject and follow the connections.

## What's Here

- **`corcoran_graph.cypher`** -- Full graph as Cypher CREATE statements (2.1 MB). Load into any Neo4j 5.x instance.
- **`metadata.json`** -- Node/relationship counts, schema, and export metadata.

## Graph Statistics

| Metric | Count |
|--------|-------|
| Total nodes | 5,276 |
| Total relationships | 9,699 |
| Node types | 14 |
| Relationship types | 29 |

### Node Types

| Label | Count | Description |
|-------|-------|-------------|
| Agent | 4,377 | Licensed real estate agents with office and market assignments |
| Source | 313 | Archived web pages, court documents, SEC filings, and other evidence |
| Person | 140 | Key individuals -- executives, attorneys, judges, public figures |
| EntryRef | 122 | Links to analytical lifestream entries (the researcher's working notes) |
| Organization | 90 | Companies, nonprofits, educational institutions, media outlets |
| Neighborhood | 73 | Office locations within markets |
| Event | 47 | Career moves, corporate actions, legal proceedings, organizational milestones |
| Market | 47 | Geographic markets (NYC, Hamptons, Chicago, etc.) |
| Region | 45 | Subregions within markets |
| Document | 7 | Court filings, corporate records |
| Brokerage | 6 | Real estate brokerages (Corcoran, Douglas Elliman, etc.) |
| Team | 4 | Agent teams within offices |
| Property | 3 | Specific real estate properties |
| LawFirm | 2 | Legal firms involved in proceedings |

### Key Relationship Types

| Relationship | Count | Meaning |
|-------------|-------|---------|
| IN_MARKET | 4,071 | Agent or office operates in a geographic market |
| IN_REGION | 2,364 | Agent or office operates in a subregion |
| WORKED_IN | 1,433 | Agent works at a specific office/neighborhood |
| DISCUSSES | 816 | EntryRef links an analytical entry to a graph entity |
| SUPPORTED_BY | 435 | **Evidence provenance** -- entity claim backed by an archived source |
| INVOLVED_IN | 129 | Person or organization participated in an event |
| AFFILIATED_WITH | 110 | Organizational or personal affiliation |
| PART_OF | 80 | Geographic or organizational hierarchy |
| EMPLOYED_BY | 77 | Employment relationship with date ranges |
| FAMILY_OF | 56 | Family connections |
| WORKED_AT | 48 | Past employment |
| MEMBER_OF | 24 | Board or organizational membership |
| COLLABORATED_WITH | 18 | Working relationship between individuals |
| RESOLVES_TO | 3 | Agent node confirmed as a known Person |

## The Provenance Model

The most important architectural feature is the SUPPORTED_BY relationship. When the system archives a web page, court document, or public record, it creates a Source node with the URL, archive path, capture date, and domain classification. Research claims are then linked to these sources via SUPPORTED_BY edges.

This means you can pick any Person, Organization, or Event node and ask: "What evidence supports this?" The answer is a traversal, not a trust exercise.

```cypher
// What sources back a specific entity?
MATCH (p:Person {name: 'Scott Durkin'})-[:SUPPORTED_BY]->(s:Source)
RETURN s.url, s.domain, s.capturedAt

// Which entities have the weakest sourcing?
MATCH (n) WHERE n:Person OR n:Organization OR n:Event
OPTIONAL MATCH (n)-[:SUPPORTED_BY]->(s:Source)
WITH n, labels(n)[0] AS type, count(s) AS sourceCount
WHERE sourceCount = 0
RETURN type, n.name, sourceCount
ORDER BY type, n.name
```

Source nodes carry a `domain` property classifying the type of evidence (e.g., `court-records`, `sec-filings`, `real-estate-media`, `mainstream-news`). This lets you filter by evidence quality -- a claim backed by court filings carries different weight than one backed by a single blog post.

## Exploring the Graph

### Start with the brokerage structure

```cypher
// Corcoran's market footprint
MATCH (a:Agent)-[:IN_MARKET]->(m:Market)
RETURN m.name, count(a) AS agents
ORDER BY agents DESC

// Office locations in a specific market
MATCH (a:Agent)-[:WORKED_IN]->(n:Neighborhood)-[:PART_OF|IN_REGION]->(r)
WHERE (a)-[:IN_MARKET]->(:Market {name: 'nyc'})
RETURN DISTINCT n.name, count(a) AS agents
ORDER BY agents DESC
```

### Follow the people

```cypher
// Key personnel and their affiliations
MATCH (p:Person)-[r:EMPLOYED_BY|AFFILIATED_WITH|INVOLVED_IN]->(target)
RETURN p.name, type(r),
       CASE WHEN target:Organization THEN target.name
            WHEN target:Event THEN target.name
            ELSE labels(target)[0] END AS connected_to
ORDER BY p.name

// Career trajectories through events
MATCH (p:Person)-[:INVOLVED_IN]->(e:Event)
RETURN p.name, e.name, e.date, e.type
ORDER BY p.name, e.date
```

### Examine the evidence chain

```cypher
// Source domain distribution
MATCH (s:Source)
RETURN s.domain, count(s) AS sources
ORDER BY sources DESC

// Most-cited sources
MATCH (s:Source)<-[:SUPPORTED_BY]-(n)
RETURN s.url, s.domain, count(n) AS entities_supported
ORDER BY entities_supported DESC
LIMIT 20

// Entities discussed in analytical entries
MATCH (er:EntryRef)-[:DISCUSSES]->(n)
RETURN labels(n)[0] AS type, n.name, count(er) AS entry_mentions
ORDER BY entry_mentions DESC
LIMIT 20
```

### Find the network structure

```cypher
// Organizations connected through shared personnel
MATCH (o1:Organization)<-[:AFFILIATED_WITH|EMPLOYED_BY]-(p:Person)-[:AFFILIATED_WITH|EMPLOYED_BY]->(o2:Organization)
WHERE id(o1) < id(o2)
RETURN o1.name, o2.name, collect(DISTINCT p.name) AS shared_people
ORDER BY size(shared_people) DESC

// Family networks
MATCH (p1:Person)-[:FAMILY_OF]->(p2:Person)
RETURN p1.name, p2.name
```

## Loading Into Neo4j

```bash
# Option 1: cypher-shell (recommended for the full 2.1 MB file)
cat corcoran_graph.cypher | cypher-shell -u neo4j -p your_password -d your_database

# Option 2: Neo4j Browser
# Open Neo4j Browser, paste the contents of corcoran_graph.cypher
# Note: large files may be slow in the browser UI
```

The export uses only CREATE statements (no MERGE), so it should be loaded into an empty database. Loading into a database with existing data will create duplicates.

After loading, you can optionally add indexes for faster queries:

```cypher
CREATE INDEX FOR (p:Person) ON (p.name);
CREATE INDEX FOR (o:Organization) ON (o.name);
CREATE INDEX FOR (e:Event) ON (e.name);
CREATE INDEX FOR (s:Source) ON (s.url);
CREATE INDEX FOR (a:Agent) ON (a.name);
CREATE INDEX FOR (m:Market) ON (m.name);
```

## Privacy and Redaction

Personal PII has been redacted: street addresses, phone numbers, and email addresses are replaced with `[redacted]` placeholders. Names of public figures and organizations are retained -- these are all subjects of public record (court documents, SEC filings, corporate registrations, journalism) and the research value depends on the data being real and verifiable.

## How This Was Built

This graph was built incrementally across 41 Cowork sessions using nicktools' research pipeline:

1. **Web research** -- search the web, read articles, follow leads
2. **Source archiving** -- every page read gets archived (local copy + Wayback Machine submission) and creates a Source node
3. **Graph commits** -- findings are committed as structured nodes (Person, Organization, Event) with SUPPORTED_BY edges wired to the archived sources
4. **Quality audits** -- periodic passes check for bare nodes (no properties), orphans (no connections), and unsupported claims (no SUPPORTED_BY edges)
5. **Cross-referencing** -- EntryRef nodes link the graph to the researcher's analytical entries, creating a bridge between structured data and narrative analysis

The 58,000+ tool calls across those sessions include web fetches, graph queries, source archiving, entity creation, and evidence wiring -- all coordinated through Claude with nicktools providing the persistent memory layer.
