#!/usr/bin/env python3
"""Neo4j Cypher query executor.

Standalone script to execute Cypher queries against any Neo4j database.
Supports both in-process dispatch (via neo4j_query_impl) and subprocess mode.
---
description: Run Cypher query against any Neo4j database
databases: [*]
---
"""

import json
import sys
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.db import get_neo4j_driver, GRAPH_DATABASE
from lib.io import output


def _serializer(obj):
    """Custom JSON serializer for Neo4j result types."""
    if hasattr(obj, "__iter__") and not isinstance(obj, (str, dict)):
        return list(obj)
    if hasattr(obj, "items"):
        return dict(obj)
    return str(obj)


def neo4j_query_impl(cypher, database=GRAPH_DATABASE, params=None, driver=None, **kwargs):
    """Execute a Cypher query and return results.

    Args:
        cypher: The Cypher query to execute
        database: Target database (default: corcoran)
        params: Query parameters dict (default: {})
        driver: Optional shared Neo4j driver

    Returns:
        dict with records, count, database
    """
    if not cypher:
        return {"error": "No cypher query provided"}

    params_dict = params or {}
    own_driver = False

    try:
        if driver is None:
            driver = get_neo4j_driver()
            own_driver = True

        with driver.session(database=database) as session:
            result = session.run(cypher, params_dict)
            records = [dict(r) for r in result]
            result.consume()

        return {
            "records": records,
            "count": len(records),
            "database": database
        }
    except Exception as e:
        return {
            "error": f"Query execution failed: {e}",
            "database": database
        }
    finally:
        if own_driver and driver:
            driver.close()


def main():
    """Subprocess entry point: read params from JSON file."""
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Missing query file path"}))
        sys.exit(1)

    try:
        with open(sys.argv[1], 'r', encoding='utf-8') as f:
            q = json.load(f)
    except Exception as e:
        print(json.dumps({"error": f"Failed to load query file: {e}"}))
        sys.exit(1)

    result = neo4j_query_impl(**q)
    output(result, serializer=_serializer)


if __name__ == "__main__":
    main()
