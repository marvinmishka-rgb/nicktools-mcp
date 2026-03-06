"""Wire SUPPORTED_BY edges from any entity to Source nodes.
---
description: Wire evidence (SUPPORTED_BY) edges to Source nodes with fuzzy URL matching
creates_edges: [SUPPORTED_BY]
databases: [GRAPH_DATABASE]
---

First-class evidence-wiring operation. Wraps lib/sources.py:wire_supported_by()
but exposed as a standalone tool instead of being buried inside each add_* tool.

Supports all entity types via optional label parameter for MATCH specificity.
Uses fuzzy URL matching against archived Source nodes.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import get_neo4j_driver, GRAPH_DATABASE
from lib.sources import wire_supported_by
from lib.schema import validate_label


def wire_evidence_impl(entity=None, entities=None, sources=None, label=None,
                        match_clause=None, extra_params=None,
                        database=GRAPH_DATABASE, driver=None, **kwargs):
    """Wire SUPPORTED_BY edges from entity(ies) to Source nodes.

    Single mode: entity="Alice", sources=[{url, confidence, claim}]
    Batch mode:  entities=[{name: "Alice", label: "Person", sources: [{...}]}, ...]

    Args:
        entity: (Single mode) Entity name to wire evidence to
        entities: (Batch mode) List of dicts, each with:
            - name: Entity name
            - label: Optional node label for MATCH specificity
            - sources: List of {url, confidence, claim} dicts
        sources: (Single mode) List of {url, confidence, claim} dicts
        label: (Single mode) Optional node label for MATCH specificity
        match_clause: (Single mode) Optional custom MATCH clause
        extra_params: (Single mode) Optional extra Cypher params
        database: Neo4j database (default: GRAPH_DATABASE)
        driver: Optional shared Neo4j driver

    Returns:
        Single mode: dict with edges_wired count and warnings list
        Batch mode: dict with per-entity results and summary
    """
    # Dispatch to batch mode if entities parameter provided
    if entities:
        return _wire_evidence_batch(entities, database=database, driver=driver)

    # Single mode
    if not entity:
        return {"error": "Missing required parameter 'entity' (or 'entities' for batch mode)"}
    if not sources:
        return {"error": "Missing required parameter 'sources' (list of {url, confidence, claim})"}
    if not isinstance(sources, list):
        return {"error": "'sources' must be a list of {url, confidence, claim} dicts"}

    # Validate label if provided
    if label:
        ok, err = validate_label(label)
        if not ok:
            return {"error": err}

    # Build match clause from label if not explicitly provided
    if not match_clause and label:
        match_clause = f"MATCH (n:{label} {{name: $name}})"

    _driver = driver or get_neo4j_driver()
    result = {"entity": entity, "label": label}

    try:
        edges_wired, warnings = wire_supported_by(
            entity_name=entity,
            sources=sources,
            match_clause=match_clause,
            extra_params=extra_params,
            database=database,
            driver=_driver,
        )
        result["edges_wired"] = edges_wired
        result["warnings"] = warnings
        result["sources_processed"] = len(sources)
    except Exception as e:
        import traceback
        return {"error": f"Evidence wiring failed: {e}", "traceback": traceback.format_exc()}
    finally:
        if not driver:
            _driver.close()

    return result


def _wire_evidence_batch(entities, database=GRAPH_DATABASE, driver=None):
    """Wire SUPPORTED_BY edges for multiple entities in one call.

    Args:
        entities: List of dicts, each with:
            - name: Entity name (required)
            - label: Optional node label
            - sources: List of {url, confidence, claim} dicts (required)
        database: Neo4j database
        driver: Shared Neo4j driver

    Returns:
        dict with per-entity results and summary
    """
    if not isinstance(entities, list):
        return {"error": "'entities' must be a list of dicts with {name, sources, ...}"}

    # Pre-validate
    validation_errors = []
    for i, ent in enumerate(entities):
        if not isinstance(ent, dict):
            validation_errors.append(f"Item {i}: not a dict")
            continue
        if not ent.get("name"):
            validation_errors.append(f"Item {i}: missing 'name'")
        if not ent.get("sources"):
            validation_errors.append(f"Item {i} ({ent.get('name', '?')}): missing 'sources'")
        elif not isinstance(ent["sources"], list):
            validation_errors.append(f"Item {i} ({ent.get('name', '?')}): 'sources' must be a list")
        ent_label = ent.get("label")
        if ent_label:
            ok, err = validate_label(ent_label)
            if not ok:
                validation_errors.append(f"Item {i} ({ent.get('name', '?')}): {err}")

    if validation_errors:
        return {
            "error": f"Pre-validation failed: {len(validation_errors)} error(s)",
            "validation_errors": validation_errors[:20],
        }

    _driver = driver or get_neo4j_driver()
    results = []
    total_wired = 0
    total_warnings = []

    try:
        for ent in entities:
            name = ent["name"]
            ent_label = ent.get("label")
            ent_sources = ent["sources"]

            match_clause = None
            if ent_label:
                match_clause = f"MATCH (n:{ent_label} {{name: $name}})"

            try:
                edges_wired, warnings = wire_supported_by(
                    entity_name=name,
                    sources=ent_sources,
                    match_clause=match_clause,
                    database=database,
                    driver=_driver,
                )
                results.append({
                    "entity": name,
                    "edges_wired": edges_wired,
                    "sources_processed": len(ent_sources),
                    "warnings": warnings,
                })
                total_wired += edges_wired
                total_warnings.extend(warnings)
            except Exception as e:
                results.append({
                    "entity": name,
                    "error": str(e),
                })
    finally:
        if not driver:
            _driver.close()

    return {
        "results": results,
        "summary": {
            "entities_processed": len(entities),
            "total_edges_wired": total_wired,
            "total_warnings": len(total_warnings),
            "errors": sum(1 for r in results if "error" in r),
        },
    }


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = wire_evidence_impl(**params)
    output(result)
