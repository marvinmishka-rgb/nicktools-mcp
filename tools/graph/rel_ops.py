"""Generic relationship operations: create, update, or remove any relationship.
---
description: Schema-driven relationship operations with label validation
creates_edges: [*]
databases: [GRAPH_DATABASE, ENTRY_DATABASE]
---

Replaces ad-hoc relationship wiring with a single validated operation.
Uses lib/schema.py for type validation and lib/db.py for modern driver access.

Actions:
  add    -- MERGE relationship between two entities. Validates type against REL_TYPES.
  update -- MATCH existing relationship, SET new properties.
  remove -- DELETE a specific relationship between two entities.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.schema import validate_rel_type, REL_TYPES
from lib.db import execute_read, execute_write, GRAPH_DATABASE
from lib.urls import canonicalize_url, VALID_PROVENANCE_TIERS


def rel_impl(action="add", type=None, from_name=None, to_name=None,
             from_label=None, to_label=None,
             props=None, database=GRAPH_DATABASE, driver=None, **kwargs):
    """Generic relationship operation: add, update, or remove.

    Args:
        action: "add" (MERGE), "update" (MATCH+SET), or "remove" (DELETE). Default: "add"
        type: Relationship type (e.g. "EMPLOYED_BY"). Alias: "rel"
        from_name: Source entity name. Alias: "from"
        to_name: Target entity name. Alias: "to"
        from_label: Optional source label for MATCH specificity (e.g. "Agent")
        to_label: Optional target label for MATCH specificity (e.g. "Organization")
        props: Dict of relationship properties
        database: Neo4j database (default: GRAPH_DATABASE)
        driver: Optional shared Neo4j driver

    Shorthand:
        graph("rel", {"from": "Alice", "rel": "KNOWS", "to": "Bob"})
        is equivalent to:
        graph("rel", {"action": "add", "type": "KNOWS", "from_name": "Alice", "to_name": "Bob"})

    Returns:
        dict with action results
    """
    # --- Alias resolution: from/to/rel → from_name/to_name/type ---
    if type is None:
        type = kwargs.pop("rel", None)
    else:
        kwargs.pop("rel", None)  # ignore duplicate

    if from_name is None:
        # "from" is a Python keyword so it arrives in **kwargs
        from_name = kwargs.pop("from", None)
    else:
        kwargs.pop("from", None)

    if to_name is None:
        to_name = kwargs.pop("to", None)
    else:
        kwargs.pop("to", None)

    # Validate required fields after alias resolution
    if not type:
        return {"error": "Missing required parameter: 'type' (or 'rel')"}
    if not from_name:
        return {"error": "Missing required parameter: 'from_name' (or 'from')"}
    if not to_name:
        return {"error": "Missing required parameter: 'to_name' (or 'to')"}

    props = dict(props or {})
    result = {"action": action, "type": type, "from": from_name, "to": to_name, "warnings": []}

    # --- Validation ---
    # Use non-strict validation: unknown rel types produce a warning and auto-register
    # rather than hard-blocking. Research naturally produces novel relationship types.
    ok, msg = validate_rel_type(type, from_label=from_label, to_label=to_label, strict=False)
    if not ok:
        return {"error": msg}
    if msg:
        result["warnings"].append(msg)

    # Handle special provenance properties
    provenance_tier = props.pop("provenanceTier", None)
    source_url = props.pop("sourceUrl", None)

    if provenance_tier:
        valid_tiers = VALID_PROVENANCE_TIERS | {"hearsay"}
        if provenance_tier not in valid_tiers:
            result["warnings"].append(
                f"Invalid provenanceTier '{provenance_tier}'. "
                f"Valid: {sorted(valid_tiers)}. Defaulting to 'web-search'."
            )
            provenance_tier = "web-search"

    if source_url:
        source_url = canonicalize_url(source_url)

    # Build MATCH clauses with optional label specificity
    from_match = f"(a:{from_label} {{name: $from_name}})" if from_label else "(a {name: $from_name})"
    to_match = f"(b:{to_label} {{name: $to_name}})" if to_label else "(b {name: $to_name})"

    # Build params
    cypher_params = {"from_name": from_name, "to_name": to_name}

    try:
        if action == "add":
            # Verify both entities exist
            check_cypher = f"MATCH {from_match} MATCH {to_match} RETURN a.name AS a, b.name AS b"
            check_records, _ = execute_read(check_cypher, database=database, driver=driver, **cypher_params)

            if not check_records:
                # Try to figure out which one is missing
                check_a, _ = execute_read(
                    f"MATCH {from_match} RETURN a.name AS name",
                    database=database, driver=driver, **{"from_name": from_name}
                )
                if not check_a:
                    return {"error": f"Entity '{from_name}' not found"}
                return {"error": f"Entity '{to_name}' not found"}

            # Build SET clause for properties
            set_parts = []
            if source_url:
                set_parts.append("r.sourceUrl = $sourceUrl")
                cypher_params["sourceUrl"] = source_url
            if provenance_tier:
                set_parts.append("r.provenanceTier = $provenanceTier")
                cypher_params["provenanceTier"] = provenance_tier
            for k, v in props.items():
                safe_k = k.replace("-", "_")
                set_parts.append(f"r.{safe_k} = ${safe_k}")
                cypher_params[safe_k] = v

            set_clause = f"SET {', '.join(set_parts)}" if set_parts else ""

            cypher = (
                f"MATCH {from_match} "
                f"MATCH {to_match} "
                f"MERGE (a)-[r:{type}]->(b) "
                f"{set_clause} "
                f"RETURN a.name AS `from`, type(r) AS rel, b.name AS `to`"
            )

            records, summary = execute_write(cypher, database=database, driver=driver, **cypher_params)

            if records:
                result["wired"] = True
                result["properties"] = {
                    **props,
                    **({"sourceUrl": source_url} if source_url else {}),
                    **({"provenanceTier": provenance_tier} if provenance_tier else {}),
                }
                result["relationships_created"] = summary.counters.relationships_created
            else:
                result["wired"] = False
                result["warnings"].append("MERGE returned no records -- unexpected")

        elif action == "update":
            set_parts = []
            if source_url:
                set_parts.append("r.sourceUrl = $sourceUrl")
                cypher_params["sourceUrl"] = source_url
            if provenance_tier:
                set_parts.append("r.provenanceTier = $provenanceTier")
                cypher_params["provenanceTier"] = provenance_tier
            for k, v in props.items():
                safe_k = k.replace("-", "_")
                set_parts.append(f"r.{safe_k} = ${safe_k}")
                cypher_params[safe_k] = v

            if not set_parts:
                return {"error": "No properties to update. Provide 'props' dict."}

            set_clause = f"SET {', '.join(set_parts)}"

            cypher = (
                f"MATCH {from_match}-[r:{type}]->{to_match} "
                f"{set_clause} "
                f"RETURN a.name AS `from`, type(r) AS rel, b.name AS `to`"
            )

            records, summary = execute_write(cypher, database=database, driver=driver, **cypher_params)

            if records:
                result["updated"] = True
                result["properties_set"] = summary.counters.properties_set
            else:
                result["updated"] = False
                result["warnings"].append(f"No {type} relationship found between '{from_name}' and '{to_name}'")

        elif action == "remove":
            cypher = (
                f"MATCH {from_match}-[r:{type}]->{to_match} "
                f"DELETE r "
                f"RETURN count(r) AS deleted"
            )

            records, summary = execute_write(cypher, database=database, driver=driver, **cypher_params)
            result["removed"] = summary.counters.relationships_deleted > 0
            result["relationships_deleted"] = summary.counters.relationships_deleted

        else:
            return {"error": f"Unknown action '{action}'. Valid: add, update, remove"}

    except Exception as e:
        import traceback
        return {"error": f"Neo4j query failed: {e}", "traceback": traceback.format_exc()}

    return result


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = rel_impl(**params)
    output(result)
