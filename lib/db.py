"""
Layer 0 -- Neo4j connection and query helpers.

No internal dependencies. Provides credentials, a driver factory,
and modern execute_query helpers with RoutingControl.

v2: Added execute_read(), execute_write(), check_query_type() using
    the neo4j v5.28+ execute_query API with automatic retry and routing.
v3: Added ensure_apoc_triggers() -- auto-repairs lifestream APOC triggers
    on server startup so Neo4j restarts don't silently break edge wiring.
"""
import os
import sys

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
if not NEO4J_PASSWORD:
    raise ValueError(
        "NEO4J_PASSWORD environment variable is required.\n"
        "Set it in your .env file: NEO4J_PASSWORD=your_password_here\n"
        "Or export it: export NEO4J_PASSWORD=your_password_here"
    )

# Configurable database names -- override via env vars for custom setups.
# Defaults are generic; existing users set NICKTOOLS_GRAPH_DB=corcoran etc.
# Actual values configured in: nicktools_mcp/.env (corcoran, lifestream)
GRAPH_DATABASE = os.getenv("NICKTOOLS_GRAPH_DB", "nicktools")
ENTRY_DATABASE = os.getenv("NICKTOOLS_ENTRY_DB", "nicktools_entries")


# Shared cache for schema_info — survives tool module reloads (lib/ isn't reloaded)
# Format: {(database, verbose): (timestamp, result_dict)}
schema_cache = {}


def get_neo4j_driver():
    """Create a Neo4j driver instance. Caller must close it.

    Existing tools use this with driver.session() + session.run().
    New tools should prefer execute_read() / execute_write().
    """
    from neo4j import GraphDatabase
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def execute_read(cypher, database=None, driver=None, **params):
    """Execute a read query with automatic routing and retry.

    Uses the modern execute_query API (neo4j v5.28+):
    - Automatic transaction management
    - Automatic retry on transient errors
    - RoutingControl.READ for read routing

    Params can be passed as kwargs: execute_read("RETURN $n AS n", n=42)

    Args:
        cypher: Cypher query string
        database: Target database (default: corcoran)
        driver: Optional shared driver (creates one if not provided)
        **params: Cypher parameters as keyword arguments

    Returns:
        (records: list[Record], summary: ResultSummary)
    """
    from neo4j import RoutingControl
    database = database or GRAPH_DATABASE
    _driver = driver or get_neo4j_driver()
    records, summary, keys = _driver.execute_query(
        cypher, database_=database, routing_=RoutingControl.READ,
        **params
    )
    return records, summary


def execute_write(cypher, database=None, driver=None, **params):
    """Execute a write query with automatic routing and retry.

    Uses the modern execute_query API (neo4j v5.28+):
    - Automatic transaction management
    - Automatic retry on transient errors
    - RoutingControl.WRITE for write routing

    Args:
        cypher: Cypher query string
        database: Target database (default: corcoran)
        driver: Optional shared driver (creates one if not provided)
        **params: Cypher parameters as keyword arguments

    Returns:
        (records: list[Record], summary: ResultSummary)
    """
    from neo4j import RoutingControl
    database = database or GRAPH_DATABASE
    _driver = driver or get_neo4j_driver()
    records, summary, keys = _driver.execute_query(
        cypher, database_=database, routing_=RoutingControl.WRITE,
        **params
    )
    return records, summary


def profile_query(cypher, database=None, driver=None, **params):
    """PROFILE a query and return execution metrics.

    Executes the query with PROFILE prefix to get actual row counts,
    db hits, and operator-level costs. Unlike EXPLAIN, this runs
    the query -- use with care on write queries.

    Args:
        cypher: Cypher query string to profile
        database: Target database
        driver: Optional shared driver
        **params: Cypher parameters

    Returns:
        dict with total_db_hits, total_rows, profile_plan
    """
    database = database or GRAPH_DATABASE
    profiled = f"PROFILE {cypher}"
    records, summary = execute_read(profiled, database=database, driver=driver, **params)
    plan = summary.profile
    if plan is None:
        return {"error": "No profile data returned (query may be write-only -- use EXPLAIN instead)"}

    def _extract_plan(p):
        return {
            "operator": p.operator_type,
            "rows": p.rows,
            "db_hits": p.db_hits,
            "children": [_extract_plan(c) for c in (p.children or [])],
        }

    return {
        "total_db_hits": plan.db_hits,
        "total_rows": plan.rows,
        "profile_plan": _extract_plan(plan),
        "result_count": len(records),
    }


def check_query_type(cypher, database=None, driver=None):
    """EXPLAIN-based safety check. Returns query_type: 'r', 'w', 'rw', or 's'.

    Uses EXPLAIN prefix so the query is never actually executed.
    Adopted from official Neo4j MCP Server v1.4.2 pattern.

    Args:
        cypher: Cypher query string to classify
        database: Target database
        driver: Optional shared driver

    Returns:
        str: 'r' (read), 'w' (write), 'rw' (read-write), 's' (schema)
    """
    database = database or GRAPH_DATABASE
    _, summary = execute_read(f"EXPLAIN {cypher}", database=database, driver=driver)
    return summary.query_type


# ============================================================
# APOC trigger definitions -- canonical source of truth
# ============================================================

# These triggers run on the lifestream database and auto-wire edges
# when StreamEntry nodes are created. They can be lost on Neo4j restart.
# ensure_apoc_triggers() reinstalls any that are missing or paused.

APOC_TRIGGERS = {
    "autoWireDomainsTags": {
        "query": """CYPHER 5 UNWIND $createdNodes AS n
   WITH n WHERE n:StreamEntry AND n.domains IS NOT NULL
   UNWIND n.domains AS d
   MERGE (dom:Domain {name: d})
   MERGE (n)-[:inDomain]->(dom)
   WITH DISTINCT n
   WHERE n.tags IS NOT NULL
   UNWIND n.tags AS t
   MERGE (tag:Tag {name: t})
   MERGE (n)-[:taggedWith]->(tag)""",
        "selector": {"phase": "afterAsync"},
    },
    "autoFollowedBy": {
        "query": """CYPHER 5 UNWIND $createdNodes AS n
   WITH n WHERE n:StreamEntry AND n.id IS NOT NULL
   OPTIONAL MATCH (prev:StreamEntry)
   WHERE prev.id < n.id AND prev.id IS NOT NULL AND prev <> n
   WITH n, prev ORDER BY prev.id DESC LIMIT 1
   WHERE prev IS NOT NULL
   MERGE (prev)-[:followedBy]->(n)""",
        "selector": {"phase": "afterAsync"},
    },
    "autoSuggestLinks": {
        "query": """CYPHER 5  UNWIND $createdNodes AS n
   WITH n WHERE n:StreamEntry AND n.domains IS NOT NULL AND size(n.domains) > 0
   MATCH (other:StreamEntry)
   WHERE other.id <> n.id
     AND other.domains IS NOT NULL
     AND NOT (n)-[:connectsTo|emergedFrom|resolves]-(other)
     AND NOT (n)-[:suggestsLink]-(other)
   WITH n, other,
        [d IN n.domains WHERE d IN other.domains] AS sd,
        [t IN COALESCE(n.tags, []) WHERE t IN COALESCE(other.tags, [])] AS st
   WITH n, other, sd, st,
        size(n.domains) + size(other.domains) - size(sd) AS du,
        size(COALESCE(n.tags, [])) + size(COALESCE(other.tags, [])) - size(st) AS tu
   WITH n, other,
        CASE WHEN du > 0 THEN toFloat(size(sd)) / du ELSE 0.0 END AS ds,
        CASE WHEN tu > 0 THEN toFloat(size(st)) / tu ELSE 0.0 END AS ts
   WITH n, other, (ds * 0.6 + ts * 0.4) AS score
   WHERE score >= 0.3
   WITH n, other, score ORDER BY score DESC LIMIT 3
   MERGE (n)-[r:suggestsLink]->(other)
   SET r.score = score, r.createdAt = datetime(), r.method = "domain-tag-jaccard\"""",
        "selector": {"phase": "afterAsync"},
    },
}


def ensure_apoc_triggers(driver=None):
    """Check and reinstall APOC triggers on the lifestream database.

    Idempotent: skips triggers that are already installed and active.
    Reinstalls triggers that are missing or paused.

    Returns:
        dict: {trigger_name: "ok" | "installed" | "unpaused" | "error: ..."}
    """
    _driver = driver or get_neo4j_driver()
    results = {}

    # Get current trigger state
    try:
        records, _ = execute_read(
            "CALL apoc.trigger.list() YIELD name, installed, paused RETURN name, installed, paused",
            database=ENTRY_DATABASE, driver=_driver
        )
        existing = {r["name"]: {"installed": r["installed"], "paused": r["paused"]} for r in records}
    except Exception as e:
        return {"_error": f"Failed to list triggers: {e}"}

    for name, defn in APOC_TRIGGERS.items():
        try:
            if name in existing and existing[name]["installed"] and not existing[name]["paused"]:
                results[name] = "ok"
                continue

            if name in existing and existing[name]["paused"]:
                # Unpause it
                execute_write(
                    "CALL apoc.trigger.resume($name)",
                    database=ENTRY_DATABASE, driver=_driver, name=name
                )
                results[name] = "unpaused"
                continue

            # Not installed -- install it
            execute_write(
                "CALL apoc.trigger.install($db, $name, $query, $selector)",
                database="system", driver=_driver,
                db=ENTRY_DATABASE, name=name,
                query=defn["query"], selector=defn["selector"]
            )
            results[name] = "installed"

        except Exception as e:
            results[name] = f"error: {e}"

    return results
