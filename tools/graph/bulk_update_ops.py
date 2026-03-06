"""Batch-update properties on multiple nodes using UNWIND.
---
description: UNWIND-based batch property updates with pre-validation
creates_nodes: []
creates_edges: []
databases: [GRAPH_DATABASE]
---

Efficient bulk property updates using a single UNWIND Cypher statement.
Designed for operations like:
  - Classifying hundreds of Source nodes' sourceType by domain
  - Bulk-setting addedDate on nodes missing it
  - Standardizing property values across many entities

Usage:
    graph("bulk_update", {
        "updates": [
            {"url": "https://nytimes.com/article", "sourceType": "primary-journalism"},
            {"url": "https://wsj.com/article", "sourceType": "primary-journalism"}
        ],
        "match_by": "url",
        "label": "Source"
    })
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import execute_write, GRAPH_DATABASE
from lib.schema import validate_label


def bulk_update_impl(updates, match_by="name", label=None, set_props=None,
                     database=GRAPH_DATABASE, driver=None, **kwargs):
    """Batch-update properties on multiple nodes using UNWIND.

    Args:
        updates: List of dicts. Each must have the match_by key + properties to SET.
                 [{name: "Alice", description: "Updated desc"}, ...]
                 OR [{url: "https://...", sourceType: "primary-journalism"}, ...]
        match_by: Property to match nodes on (default "name"). Use "url" for Sources.
        label: Optional label filter for the MATCH clause. Recommended for safety.
        set_props: Optional list of property names to SET from each update dict.
                   If None, all keys except match_by are SET.
        database: Neo4j database (default: GRAPH_DATABASE)
        driver: Optional shared Neo4j driver

    Returns:
        dict with matched count, property keys updated, and any validation warnings
    """
    if not updates:
        return {"error": "Missing 'updates' parameter. Provide a list of dicts."}
    if not isinstance(updates, list):
        return {"error": "'updates' must be a list of dicts."}

    # Validate label if provided
    if label:
        ok, err = validate_label(label)
        if not ok:
            return {"error": err}

    # Pre-validate all update dicts
    validation_errors = []
    for i, upd in enumerate(updates):
        if not isinstance(upd, dict):
            validation_errors.append(f"Item {i}: not a dict")
            continue
        if match_by not in upd:
            validation_errors.append(f"Item {i}: missing match key '{match_by}'")
            continue
        # Must have at least one property to set beyond the match key
        other_keys = [k for k in upd if k != match_by]
        if set_props:
            other_keys = [k for k in other_keys if k in set_props]
        if not other_keys:
            validation_errors.append(f"Item {i}: no properties to SET (only has '{match_by}')")

    if validation_errors:
        return {
            "error": f"Pre-validation failed: {len(validation_errors)} error(s)",
            "validation_errors": validation_errors[:20],
            "total_updates": len(updates),
        }

    # Determine which properties to SET
    if set_props:
        prop_keys = set_props
    else:
        # Collect all unique keys across all update dicts (excluding match_by)
        prop_keys = sorted(set(
            k for upd in updates for k in upd if k != match_by
        ))

    if not prop_keys:
        return {"error": "No properties to SET after filtering."}

    # Build UNWIND Cypher
    label_clause = f":{label}" if label else ""
    set_parts = [f"n.{k} = upd.{k}" for k in prop_keys]
    set_clause = ", ".join(set_parts)

    query = f"""
        UNWIND $updates AS upd
        MATCH (n{label_clause} {{{match_by}: upd.{match_by}}})
        SET {set_clause}
        RETURN count(n) AS matched
    """

    try:
        records, summary = execute_write(
            query, database=database, driver=driver, updates=updates
        )
        matched = records[0]["matched"] if records else 0

        return {
            "matched": matched,
            "total_updates": len(updates),
            "properties_set": prop_keys,
            "match_by": match_by,
            "label": label,
            "unmatched_estimate": len(updates) - matched if matched < len(updates) else 0,
        }
    except Exception as e:
        import traceback
        return {
            "error": f"Bulk update failed: {e}",
            "traceback": traceback.format_exc(),
        }


if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = bulk_update_impl(**params)
    output(result)
