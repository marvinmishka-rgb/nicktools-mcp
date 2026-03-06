"""Entity deduplication: scan for similar entities and merge confirmed duplicates.
---
description: Find and merge duplicate entities using fuzzy name matching and APOC mergeNodes
creates_nodes: []
creates_edges: []
databases: [corcoran]
---

Scan mode: Uses difflib.SequenceMatcher to find entity name pairs above a similarity
threshold within the same label. Boosts confidence with shared relationships.

Merge mode: Uses apoc.refactor.mergeNodes() to combine two nodes, preserving all
relationships and properties from both. Requires explicit entity names -- no auto-merge.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import execute_read, execute_write, GRAPH_DATABASE


def _scan_duplicates(label, threshold, database, driver):
    """Find candidate duplicate pairs within a label using fuzzy name matching.

    Args:
        label: Node label to scan (e.g. "Person", "Event")
        threshold: Minimum similarity score (0.0-1.0)
        database: Neo4j database
        driver: Neo4j driver

    Returns:
        list of candidate dicts sorted by similarity descending
    """
    from difflib import SequenceMatcher

    # Get all entity names for this label
    cypher = f"MATCH (n:{label}) RETURN n.name AS name ORDER BY n.name"
    records, _ = execute_read(cypher, database=database, driver=driver)
    names = [r["name"] for r in records if r["name"]]

    candidates = []
    # O(n^2) comparison -- fine for our graph sizes (<200 entities per label)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            ratio = SequenceMatcher(None, names[i].lower(), names[j].lower()).ratio()
            if ratio >= threshold:
                candidates.append({
                    "a": names[i],
                    "b": names[j],
                    "similarity": round(ratio, 3),
                    "label": label,
                })

    # Boost confidence with shared relationships
    for cand in candidates:
        boost = _relationship_overlap(cand["a"], cand["b"], database, driver)
        cand["shared_connections"] = boost["shared"]
        cand["boosted_similarity"] = round(
            min(1.0, cand["similarity"] + (0.1 * len(boost["shared"]))), 3
        )

    # Sort by boosted similarity descending
    candidates.sort(key=lambda c: c["boosted_similarity"], reverse=True)
    return candidates


def _relationship_overlap(name_a, name_b, database, driver):
    """Find shared relationship targets between two entities.

    Returns dict with shared connections list.
    """
    cypher = """
    MATCH (a {name: $name_a})-[r1]-(shared)-[r2]-(b {name: $name_b})
    WHERE a <> b AND a <> shared AND b <> shared
    RETURN DISTINCT shared.name AS shared_name, labels(shared)[0] AS shared_label
    LIMIT 10
    """
    records, _ = execute_read(
        cypher, database=database, driver=driver,
        name_a=name_a, name_b=name_b
    )
    return {
        "shared": [
            {"name": r["shared_name"], "label": r["shared_label"]}
            for r in records if r["shared_name"]
        ]
    }


def _merge_entities(keep_name, remove_name, merge_properties, database, driver):
    """Merge two entities using apoc.refactor.mergeNodes().

    The 'keep' node survives. All relationships from 'remove' are transferred to 'keep'.
    Properties from 'remove' that don't exist on 'keep' are copied over.

    Args:
        keep_name: Name of the node to keep
        remove_name: Name of the node to remove
        merge_properties: If True, copy properties from remove to keep (keep wins on conflicts)
        database: Neo4j database
        driver: Neo4j driver

    Returns:
        dict with merge results
    """
    # First, verify both nodes exist and get their details
    check_cypher = """
    MATCH (keep {name: $keep_name})
    MATCH (remove {name: $remove_name})
    RETURN keep.name AS keep_name, labels(keep) AS keep_labels,
           remove.name AS remove_name, labels(remove) AS remove_labels,
           size([(keep)-[r]-() | r]) AS keep_rels,
           size([(remove)-[r]-() | r]) AS remove_rels
    """
    records, _ = execute_read(
        check_cypher, database=database, driver=driver,
        keep_name=keep_name, remove_name=remove_name
    )

    if not records:
        # Figure out which one is missing
        check_a, _ = execute_read(
            "MATCH (n {name: $name}) RETURN n.name", database=database, driver=driver, name=keep_name)
        if not check_a:
            return {"error": f"Keep entity '{keep_name}' not found"}
        return {"error": f"Remove entity '{remove_name}' not found"}

    rec = records[0]
    pre_merge = {
        "keep": {"name": rec["keep_name"], "labels": rec["keep_labels"], "relationships": rec["keep_rels"]},
        "remove": {"name": rec["remove_name"], "labels": rec["remove_labels"], "relationships": rec["remove_rels"]},
    }

    # Merge properties strategy: "combine" preserves arrays, "keep" node wins on conflicts
    merge_config = '{properties: "combine", mergeRels: true}'

    merge_cypher = f"""
    MATCH (keep {{name: $keep_name}})
    MATCH (remove {{name: $remove_name}})
    WITH keep, remove
    CALL apoc.refactor.mergeNodes([keep, remove], {merge_config})
    YIELD node
    RETURN node.name AS name, labels(node) AS labels,
           size([(node)-[r]-() | r]) AS total_rels,
           properties(node) AS props
    """

    try:
        records, summary = execute_write(
            merge_cypher, database=database, driver=driver,
            keep_name=keep_name, remove_name=remove_name
        )
    except Exception as e:
        return {"error": f"Merge failed: {e}. Is APOC installed and enabled?"}

    if not records:
        return {"error": "Merge returned no results -- unexpected"}

    merged = records[0]

    # Fix name property -- APOC "combine" turns scalars into arrays when both nodes
    # have the same property. Restore name to a string and add removed name as altName.
    fix_cypher = """
    MATCH (n)
    WHERE n.name = $keep_name OR n.name = [$keep_name, $remove_name] OR n.name = [$remove_name, $keep_name]
    SET n.name = $keep_name,
        n.altNames = CASE
            WHEN n.altNames IS NULL THEN [$remove_name]
            WHEN NOT $remove_name IN n.altNames THEN n.altNames + $remove_name
            ELSE n.altNames
        END
    RETURN n.name AS name, n.altNames AS altNames
    """
    execute_write(
        fix_cypher, database=database, driver=driver,
        keep_name=keep_name, remove_name=remove_name
    )

    return {
        "merged": True,
        "surviving_node": {
            "name": merged["name"],
            "labels": merged["labels"],
            "total_relationships": merged["total_rels"],
        },
        "pre_merge": pre_merge,
        "alt_name_added": remove_name,
    }


def deduplicate_impl(action, label=None, threshold=0.7, keep=None, remove=None,
                     merge_properties=True, database=GRAPH_DATABASE, driver=None, **kwargs):
    """Entity deduplication: scan for candidates or merge confirmed duplicates.

    Args:
        action: "scan" (find candidates) or "merge" (combine two entities)
        label: For scan: node label to scan (e.g. "Person", "Event"). Scans all if omitted.
        threshold: For scan: minimum similarity score (default: 0.7)
        keep: For merge: name of the node to keep
        remove: For merge: name of the node to merge into keep and delete
        merge_properties: For merge: copy properties from remove to keep (default: True)
        database: Neo4j database (default: corcoran)
        driver: Shared Neo4j driver

    Returns:
        dict with scan candidates or merge results
    """
    if action == "scan":
        labels_to_scan = []
        if label:
            labels_to_scan = [label]
        else:
            # Scan common entity labels
            labels_to_scan = ["Person", "Organization", "Event"]

        all_candidates = []
        for lbl in labels_to_scan:
            candidates = _scan_duplicates(lbl, threshold, database, driver)
            all_candidates.extend(candidates)

        # Sort all by boosted similarity
        all_candidates.sort(key=lambda c: c["boosted_similarity"], reverse=True)

        return {
            "action": "scan",
            "labels_scanned": labels_to_scan,
            "threshold": threshold,
            "candidates": all_candidates,
            "total_candidates": len(all_candidates),
        }

    elif action == "merge":
        if not keep:
            return {"error": "Missing 'keep' parameter -- name of the entity to keep"}
        if not remove:
            return {"error": "Missing 'remove' parameter -- name of the entity to merge and delete"}

        result = _merge_entities(keep, remove, merge_properties, database, driver)
        result["action"] = "merge"
        return result

    else:
        return {"error": f"Unknown action '{action}'. Valid: scan, merge"}


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = deduplicate_impl(**params)
    output(result)
