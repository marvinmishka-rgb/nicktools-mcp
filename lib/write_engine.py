"""
Layer 2 -- Unified graph write engine.

Depends on: lib.schema (Layer 0), lib.db (Layer 0), lib.urls (Layer 1),
            lib.sources (Layer 2).

Accepts entities in natural shapes (with nested relationships and sources),
validates against the schema registry, generates optimized Cypher, and
executes batched writes. Replaces the 6 separate write paths (node_ops,
add_person, add_organization, add_event, add_document, add_property) with
a single interface.

Design principles:
  - Natural input shapes: nested dicts for relationships/sources, flat props
  - Schema-driven: all labels, rel types, and properties validated before execution
  - Batched: UNWIND for multi-entity commits where possible
  - Evidence auto-wiring: sources on entities become SUPPORTED_BY edges
  - Backward compatible: old add_person etc. can delegate here

Usage:
    from lib.write_engine import write_entities

    # Simple entity
    write_entities([{"label": "Person", "name": "Alice Chen"}], driver=driver)

    # Entity with relationships and sources
    write_entities([{
        "label": "Person", "name": "Alice Chen",
        "description": "Real estate advisor",
        "relationships": [
            {"type": "EMPLOYED_BY", "target": "The Agency",
             "target_label": "Organization", "props": {"role": "Senior Agent"}}
        ],
        "sources": [{"url": "https://...", "confidence": "archived-verified"}]
    }], driver=driver)
"""
from lib.schema import (
    validate_label, validate_required_props, validate_props,
    validate_rel_type, get_merge_key, get_auto_set, NODE_TYPES, REL_TYPES
)
from lib.db import execute_read, execute_write, GRAPH_DATABASE
from lib.sources import wire_supported_by


# ============================================================
# Input normalization
# ============================================================

def _normalize_entity(entity):
    """Normalize an entity dict into a standard internal shape.

    Extracts and separates:
      - flat_props: Neo4j-safe properties (primitives/arrays)
      - relationships: list of relationship dicts
      - sources: list of source dicts for SUPPORTED_BY wiring
      - extra_labels: additional labels to apply

    Args:
        entity: Raw entity dict from caller

    Returns:
        (label, flat_props, relationships, sources, extra_labels, warnings)
        or raises ValueError on invalid input.
    """
    if not isinstance(entity, dict):
        raise ValueError(f"Entity must be a dict, got {type(entity).__name__}")

    label = entity.get("label")
    if not label:
        raise ValueError("Entity missing required 'label' key")

    ok, err = validate_label(label)
    if not ok:
        raise ValueError(err)

    # Extract structured fields before flattening
    relationships = entity.get("relationships", [])
    sources = entity.get("sources", [])
    extra_labels = entity.get("extra_labels", [])
    if isinstance(extra_labels, str):
        extra_labels = [extra_labels]

    # Everything else is a property candidate
    reserved_keys = {"label", "relationships", "sources", "extra_labels", "database", "driver"}
    raw_props = {k: v for k, v in entity.items() if k not in reserved_keys and v is not None}

    # Flatten nested dicts: if a value is a dict, it's probably misstructured.
    # Reject with a helpful error instead of silently losing data.
    warnings = []
    flat_props = {}
    for k, v in raw_props.items():
        if isinstance(v, dict):
            raise ValueError(
                f"Property '{k}' is a nested dict. Neo4j properties must be primitives "
                f"or arrays. If this is a relationship, put it in 'relationships'. "
                f"If these are extra properties, flatten them to top-level keys."
            )
        flat_props[k] = v

    # Validate required props
    merge_keys = get_merge_key(label)
    missing_merge = [k for k in merge_keys if k not in flat_props]
    if missing_merge:
        raise ValueError(f"Entity {label} missing merge key(s): {missing_merge}")

    ok, err = validate_required_props(label, flat_props)
    if not ok:
        raise ValueError(err)

    # Validate property names (warnings only for extra_props=True labels)
    ok, prop_warnings = validate_props(label, flat_props)
    if not ok:
        raise ValueError(prop_warnings[0] if prop_warnings else "Property validation failed")
    warnings.extend(prop_warnings)

    # Validate relationships
    for i, rel in enumerate(relationships):
        if not isinstance(rel, dict):
            raise ValueError(f"relationships[{i}] must be a dict")
        rel_type = rel.get("type")
        if not rel_type:
            raise ValueError(f"relationships[{i}] missing 'type'")
        target = rel.get("target")
        if not target:
            raise ValueError(f"relationships[{i}] missing 'target' (target entity name)")
        ok, err = validate_rel_type(rel_type)
        if not ok:
            raise ValueError(f"relationships[{i}]: {err}")

    return label, flat_props, relationships, sources, extra_labels, warnings


# ============================================================
# Cypher generation
# ============================================================

def _build_node_cypher(label, flat_props):
    """Build MERGE Cypher for a single node.

    Returns (cypher, params) tuple.
    """
    merge_keys = get_merge_key(label)
    auto_set = get_auto_set(label)

    # Build merge clause
    merge_parts = [f"{k}: $merge_{k}" for k in merge_keys]
    merge_clause = ", ".join(merge_parts)

    # Separate merge-key props from update props
    update_props = {k: v for k, v in flat_props.items() if k not in merge_keys and v is not None}

    params = {}
    for k in merge_keys:
        params[f"merge_{k}"] = flat_props[k]

    if update_props:
        params["update_props"] = update_props

    auto_clauses = ", ".join(f"n.{k} = {v}" for k, v in auto_set.items())

    lines = [f"MERGE (n:{label} {{{merge_clause}}})"]

    # ON CREATE: set auto fields + all provided props
    on_create_parts = []
    if auto_clauses:
        on_create_parts.append(auto_clauses)
    if update_props:
        on_create_parts.append("n += $update_props")
    if on_create_parts:
        lines.append(f"ON CREATE SET {', '.join(on_create_parts)}")

    # ON MATCH: set provided props (not auto fields)
    if update_props:
        lines.append("ON MATCH SET n += $update_props")

    lines.append("RETURN n, labels(n) AS labels")

    return "\n".join(lines), params


def _build_rel_cypher(from_name, rel, from_label=None):
    """Build MERGE Cypher for a single relationship.

    The target entity is MERGEd (created if missing) to avoid
    ordering dependency in batch commits.

    Returns (cypher, params) tuple.
    """
    rel_type = rel["type"]
    target_name = rel["target"]
    target_label = rel.get("target_label", "")
    rel_props = rel.get("props", {})

    # Build from-match with label if available
    from_match = f"(a:{from_label} {{name: $from_name}})" if from_label else "(a {name: $from_name})"

    # Target: MERGE to auto-create if not exists
    if target_label:
        target_clause = f"(b:{target_label} {{name: $to_name}})"
    else:
        target_clause = "(b {name: $to_name})"

    params = {"from_name": from_name, "to_name": target_name}

    # Build property SET clause
    set_parts = []
    for k, v in rel_props.items():
        safe_k = k.replace("-", "_")
        set_parts.append(f"r.{safe_k} = $rel_{safe_k}")
        params[f"rel_{safe_k}"] = v

    set_clause = f"SET {', '.join(set_parts)}" if set_parts else ""

    cypher = (
        f"MATCH {from_match} "
        f"MERGE {target_clause} "
        f"MERGE (a)-[r:{rel_type}]->(b) "
        f"{set_clause} "
        f"RETURN a.name AS `from`, type(r) AS rel, b.name AS `to`"
    )

    return cypher, params


def _build_label_cypher(name, labels, label=None):
    """Build Cypher to add extra labels to a node.

    Returns (cypher, params) tuple.
    """
    match = f"(n:{label} {{name: $name}})" if label else "(n {name: $name})"
    label_strs = [l.replace(" ", "").replace("-", "") for l in labels]
    set_clause = ":".join(label_strs)
    return f"MATCH {match} SET n:{set_clause}", {"name": name}


# ============================================================
# Main write function
# ============================================================

def write_entities(entities, database=GRAPH_DATABASE, driver=None):
    """Write a batch of entities to the graph.

    Each entity can include nested relationships and sources. The engine:
    1. Validates all entities against the schema (fail-fast on errors)
    2. Creates/merges all entity nodes
    3. Wires all relationships (MERGEing target nodes as needed)
    4. Applies extra labels
    5. Wires SUPPORTED_BY edges for any entity with sources

    Args:
        entities: List of entity dicts. Each must have 'label' and merge key(s).
            Optional: 'relationships', 'sources', 'extra_labels', plus any
            node properties as top-level keys.
        database: Neo4j database (default: corcoran)
        driver: Shared Neo4j driver (required for in-process dispatch)

    Returns:
        dict with:
        - entities: list of per-entity results
        - summary: aggregate counts
        - warnings: collected warnings
    """
    if not entities:
        return {"error": "No entities provided"}
    if not isinstance(entities, list):
        return {"error": "'entities' must be a list of entity dicts"}

    # Phase 1: Validate all entities before executing any writes
    normalized = []
    all_warnings = []
    validation_errors = []

    for i, entity in enumerate(entities):
        try:
            label, flat_props, rels, sources, extra_labels, warnings = _normalize_entity(entity)
            normalized.append({
                "index": i,
                "label": label,
                "flat_props": flat_props,
                "relationships": rels,
                "sources": sources,
                "extra_labels": extra_labels,
            })
            all_warnings.extend(warnings)
        except ValueError as e:
            validation_errors.append({"index": i, "error": str(e)})

    if validation_errors:
        return {
            "validation_errors": validation_errors,
            "summary": {"total": len(entities), "completed": 0, "errors": len(validation_errors)},
            "message": f"Pre-validation failed: {len(validation_errors)} error(s). No writes executed."
        }

    # Phase 2: Execute writes
    results = []
    summary = {
        "total": len(entities),
        "nodes_created": 0,
        "nodes_updated": 0,
        "relationships_wired": 0,
        "evidence_wired": 0,
        "labels_added": 0,
        "errors": 0,
    }

    for norm in normalized:
        entity_result = {
            "index": norm["index"],
            "label": norm["label"],
            "name": norm["flat_props"].get(get_merge_key(norm["label"])[0], "unknown"),
        }

        try:
            # Step 1: Create/merge the node
            cypher, params = _build_node_cypher(norm["label"], norm["flat_props"])
            records, node_summary = execute_write(cypher, database=database, driver=driver, **params)

            if records:
                counters = node_summary.counters
                if counters.nodes_created > 0:
                    entity_result["created"] = True
                    summary["nodes_created"] += 1
                else:
                    entity_result["created"] = False
                    entity_result["updated"] = True
                    summary["nodes_updated"] += 1
                entity_result["properties_set"] = counters.properties_set

            # Step 2: Apply extra labels
            if norm["extra_labels"]:
                label_cypher, label_params = _build_label_cypher(
                    entity_result["name"], norm["extra_labels"], norm["label"]
                )
                execute_write(label_cypher, database=database, driver=driver, **label_params)
                entity_result["labels_added"] = norm["extra_labels"]
                summary["labels_added"] += len(norm["extra_labels"])

            # Step 3: Wire relationships
            rels_wired = 0
            for rel in norm["relationships"]:
                rel_cypher, rel_params = _build_rel_cypher(
                    entity_result["name"], rel, from_label=norm["label"]
                )
                try:
                    execute_write(rel_cypher, database=database, driver=driver, **rel_params)
                    rels_wired += 1
                except Exception as e:
                    all_warnings.append(
                        f"Rel {rel['type']} -> {rel['target']} failed: {e}"
                    )

            entity_result["relationships_wired"] = rels_wired
            summary["relationships_wired"] += rels_wired

            # Step 4: Wire evidence (SUPPORTED_BY edges)
            if norm["sources"]:
                match_clause = f"MATCH (n:{norm['label']} {{name: $name}})"
                edges_wired, source_warnings = wire_supported_by(
                    entity_name=entity_result["name"],
                    sources=norm["sources"],
                    match_clause=match_clause,
                    database=database,
                    driver=driver,
                )
                entity_result["evidence_wired"] = edges_wired
                summary["evidence_wired"] += edges_wired
                all_warnings.extend(source_warnings)

        except Exception as e:
            import traceback
            entity_result["error"] = str(e)
            entity_result["traceback"] = traceback.format_exc()
            summary["errors"] += 1

        results.append(entity_result)

    return {
        "entities": results,
        "summary": summary,
        "warnings": all_warnings,
    }
