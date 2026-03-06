"""Validated Cypher passthrough for APOC calls and advanced queries.
---
description: Execute arbitrary Cypher with EXPLAIN-based read safety validation
creates_nodes: []
creates_edges: []
databases: [corcoran, lifestream, planttaxonomy]
---

Provides a validated passthrough for Cypher queries that don't fit the
node/rel/wire_evidence pattern -- APOC procedures, complex traversals,
aggregate queries, etc.

Modes:
  read  -- EXPLAIN-validated read-only. Rejects write queries.
  write -- Executes any query. Use for APOC refactoring, batch updates, etc.
  auto  -- Classifies via EXPLAIN, routes to read or write automatically.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import execute_read, execute_write, check_query_type, GRAPH_DATABASE


# Maximum records to return (prevents accidental memory blowup)
DEFAULT_MAX_RECORDS = 1000


def cypher_impl(query, mode="auto", database=GRAPH_DATABASE, params=None,
                max_records=DEFAULT_MAX_RECORDS, driver=None, **kwargs):
    """Execute a Cypher query with optional read-safety validation.

    Args:
        query: Cypher query string. Supports APOC, GDS, and standard Cypher.
        mode: Execution mode:
            - "read": EXPLAIN-validated read-only. Rejects write queries.
            - "write": Executes any query without safety check.
            - "auto" (default): Classifies via EXPLAIN, routes accordingly.
        database: Neo4j database (default: corcoran)
        params: Optional dict of Cypher parameters (e.g. {"name": "John"})
        max_records: Maximum records to return (default: 1000). Set to 0 for unlimited.
        driver: Optional shared Neo4j driver

    Returns:
        dict with:
        - records: List of result dicts
        - record_count: Number of records returned
        - truncated: True if max_records limit was hit
        - query_type: Classified type ('r', 'w', 'rw', 's')
        - counters: Write counters (if applicable)
        - mode: The execution mode used
    """
    if not query or not query.strip():
        return {"error": "Missing 'query' parameter. Provide a Cypher query string."}

    valid_modes = ("read", "write", "auto")
    if mode not in valid_modes:
        return {"error": f"Invalid mode '{mode}'. Must be: {', '.join(valid_modes)}"}

    params = params or {}
    result = {"mode": mode, "database": database}

    try:
        # --- Classify query ---
        if mode in ("read", "auto"):
            query_type = check_query_type(query, database=database, driver=driver)
            result["query_type"] = query_type

            if mode == "read" and query_type != "r":
                return {
                    "error": f"Query classified as '{query_type}' (not read-only). "
                             f"Use mode='write' or mode='auto' to execute write queries.",
                    "query_type": query_type,
                    "mode": mode,
                    "hint": "EXPLAIN-based classification detected this query would modify data."
                }

            # Auto mode: route based on classification
            if mode == "auto":
                mode = "read" if query_type == "r" else "write"
                result["auto_routed_to"] = mode

        # --- Execute ---
        if mode == "read":
            records, summary = execute_read(
                query, database=database, driver=driver, **params
            )
        else:
            records, summary = execute_write(
                query, database=database, driver=driver, **params
            )
            # Capture query_type for write mode (wasn't classified above)
            if "query_type" not in result:
                result["query_type"] = summary.query_type

        # --- Format results ---
        truncated = False
        if max_records > 0 and len(records) > max_records:
            records = records[:max_records]
            truncated = True

        # Convert records to plain dicts
        result_records = []
        for record in records:
            row = {}
            for key in record.keys():
                val = record[key]
                # Handle Neo4j Node objects
                if hasattr(val, 'labels') and hasattr(val, 'items'):
                    row[key] = {
                        "_labels": list(val.labels),
                        **dict(val.items())
                    }
                # Handle Neo4j Relationship objects
                elif hasattr(val, 'type') and hasattr(val, 'items'):
                    row[key] = {
                        "_type": val.type,
                        **dict(val.items())
                    }
                # Handle lists of Neo4j objects
                elif isinstance(val, list):
                    row[key] = [
                        {"_labels": list(v.labels), **dict(v.items())}
                        if hasattr(v, 'labels') and hasattr(v, 'items')
                        else {"_type": v.type, **dict(v.items())}
                        if hasattr(v, 'type') and hasattr(v, 'items')
                        else v
                        for v in val
                    ]
                else:
                    row[key] = val
            result_records.append(row)

        result["records"] = result_records
        result["record_count"] = len(result_records)
        result["truncated"] = truncated

        # Include write counters if any mutations occurred
        counters = summary.counters
        counter_dict = {
            "nodes_created": counters.nodes_created,
            "nodes_deleted": counters.nodes_deleted,
            "relationships_created": counters.relationships_created,
            "relationships_deleted": counters.relationships_deleted,
            "properties_set": counters.properties_set,
            "labels_added": counters.labels_added,
            "labels_removed": counters.labels_removed,
        }
        # Only include counters if something changed
        if any(v > 0 for v in counter_dict.values()):
            result["counters"] = counter_dict

    except Exception as e:
        import traceback
        return {
            "error": f"Cypher execution failed: {e}",
            "traceback": traceback.format_exc(),
            "mode": mode,
            "database": database,
        }

    return result


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = cypher_impl(**params)
    output(result)
