"""Generic node operations: create, update, or inspect any node type.
---
description: Schema-driven node operations for any label in the registry
creates_nodes: [Person, Agent, Organization, Event, Document, Property]
creates_edges: []
databases: [corcoran, lifestream]
---

Replaces per-type add_* tools with a single generic operation.
Uses lib/schema.py for validation and Cypher generation,
and lib/db.py execute_write()/execute_read() for modern driver access.

Actions:
  add    -- MERGE by merge_key, set properties. ON CREATE sets auto_set fields.
  update -- MATCH existing node, SET properties. Fails if node doesn't exist.
  get    -- Return node with all properties and immediate relationships.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.schema import (
    validate_label, validate_required_props, validate_props,
    get_merge_key, build_merge_cypher, NODE_TYPES
)
from lib.db import execute_read, execute_write, GRAPH_DATABASE


def node_impl(action, label, database=GRAPH_DATABASE, driver=None, **kwargs):
    """Generic node operation: add, update, or get any node type.

    Args:
        action: "add" (MERGE), "update" (MATCH+SET), or "get" (read-only)
        label: Node label (e.g. "Agent", "Person", "Organization")
        database: Neo4j database (default: corcoran)
        driver: Optional shared Neo4j driver
        **kwargs: All node properties (name, description, etc.)
            For add/update: properties to set on the node
            For get: merge key properties to identify the node

    Returns:
        dict with action results:
        - add: {action, label, name, created, properties_set, warnings}
        - update: {action, label, name, found, properties_set, warnings}
        - get: {action, label, name, found, properties, relationships}
    """
    result = {"action": action, "label": label, "warnings": []}

    # --- Validation ---
    ok, err = validate_label(label)
    if not ok:
        return {"error": err}

    # Extract merge key values from kwargs
    merge_keys = get_merge_key(label)
    merge_vals = {k: kwargs.get(k) for k in merge_keys}

    # Check required merge keys are present
    missing_merge = [k for k, v in merge_vals.items() if v is None]
    if missing_merge:
        return {"error": f"Missing merge key(s) for {label}: {missing_merge}. "
                         f"Merge key(s): {merge_keys}"}

    # For add action, check all required props
    if action == "add":
        ok, err = validate_required_props(label, {**merge_vals, **kwargs})
        if not ok:
            return {"error": err}

    # Build props dict (everything except internal params)
    internal_keys = {"action", "label", "database", "driver", "timeout_seconds"}
    props = {k: v for k, v in kwargs.items() if k not in internal_keys and v is not None}

    # Reject nested dicts -- Neo4j properties must be primitives or arrays of primitives.
    # A nested dict here means either (a) caller used "properties" instead of "props" in
    # a commit operation, or (b) a complex structure wasn't flattened before calling node_impl.
    nested_keys = [k for k, v in props.items() if isinstance(v, dict)]
    if nested_keys:
        return {"error": f"Nested dict values not allowed as Neo4j properties: {nested_keys}. "
                         f"Neo4j properties must be primitives or arrays of primitives. "
                         f"Flatten these into individual top-level keys, or use 'props' "
                         f"(not 'properties') in commit operations."}

    # Validate properties
    ok, warnings = validate_props(label, props)
    if not ok:
        return {"error": warnings[0] if warnings else "Property validation failed"}
    result["warnings"] = warnings

    # Set name/identifier for result reporting
    primary_key = merge_keys[0]
    result["name"] = merge_vals.get(primary_key, "unknown")

    # --- Execute ---
    try:
        cypher, cypher_params = build_merge_cypher(label, props, action=action)
    except ValueError as e:
        return {"error": str(e)}

    try:
        if action == "get":
            records, summary = execute_read(
                cypher, database=database, driver=driver, **cypher_params
            )
        else:
            records, summary = execute_write(
                cypher, database=database, driver=driver, **cypher_params
            )
    except Exception as e:
        return {"error": f"Neo4j query failed: {e}"}

    # --- Format results ---
    if action == "add":
        if records:
            node = records[0]["n"]
            node_labels = records[0]["labels"]
            counters = summary.counters
            result["created"] = counters.nodes_created > 0
            result["updated"] = not result["created"]
            result["properties_set"] = counters.properties_set
            result["labels"] = node_labels
        else:
            result["created"] = False
            result["updated"] = False
            result["warnings"].append("MERGE returned no records -- unexpected")

    elif action == "update":
        if records:
            node = records[0]["n"]
            counters = summary.counters
            result["found"] = True
            result["properties_set"] = counters.properties_set
            result["labels"] = records[0]["labels"]
        else:
            result["found"] = False
            result["warnings"].append(f"No {label} found with {merge_vals}")

    elif action == "get":
        if records:
            node = records[0]["n"]
            result["found"] = True
            result["properties"] = dict(node)
            result["labels"] = records[0]["labels"]
            result["relationships"] = records[0].get("relationships", [])
        else:
            result["found"] = False

    return result


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = node_impl(**params)
    output(result)
