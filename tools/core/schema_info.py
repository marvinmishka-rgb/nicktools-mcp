"""Database schema discovery tool.
---
description: Get schema info for any Neo4j database (labels, properties, relationships, indexes, counts)
databases: [GRAPH_DATABASE, ENTRY_DATABASE, planttaxonomy]
---

Returns a concise schema overview: node labels with properties and counts,
relationship types, and index info. Designed to be compact enough that the
LLM can absorb it in one read.

Default output is compact (property names, collapsed relationships).
Use verbose=true for full property types and expanded relationship details.
"""
import sys
import time
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.db import execute_read, GRAPH_DATABASE, schema_cache


CACHE_TTL = 300  # 5 minutes


def schema_info_impl(database=GRAPH_DATABASE, verbose=False, force=False, driver=None, **kwargs):
    """Get schema info for a Neo4j database.

    Args:
        database: Target database (default: GRAPH_DATABASE)
        verbose: If True, include property types and full relationship details
        force: If True, bypass cache and re-fetch
        driver: Optional shared Neo4j driver

    Returns:
        dict with nodes, relationships, indexes, summary
    """
    cache_key = (database, verbose)
    if not force and cache_key in schema_cache:
        cached_at, cached_result = schema_cache[cache_key]
        if time.time() - cached_at < CACHE_TTL:
            out = dict(cached_result)
            out["cached"] = True
            out["cache_age_seconds"] = int(time.time() - cached_at)
            return out

    result = {"database": database, "cached": False}

    try:
        # --- Node labels with counts ---
        records, _ = execute_read(
            "CALL db.labels() YIELD label "
            "CALL apoc.cypher.run('MATCH (n:`' + label + '`) RETURN count(n) AS c', {}) YIELD value "
            "RETURN label, value.c AS count ORDER BY label",
            database=database, driver=driver
        )
        label_counts = {r["label"]: r["count"] for r in records}

        # --- Schema from apoc.meta.schema ---
        records, _ = execute_read(
            "CALL apoc.meta.schema() YIELD value RETURN value",
            database=database, driver=driver
        )
        if not records:
            result["error"] = "apoc.meta.schema() returned no data"
            return result

        raw_schema = records[0]["value"]
        nodes = {}
        rel_types = {}

        for key, meta in raw_schema.items():
            if not isinstance(meta, dict):
                continue
            schema_type = meta.get("type", "")

            if schema_type == "node":
                count = label_counts.get(key, 0)
                raw_props = meta.get("properties", {})
                raw_rels = meta.get("relationships", {})

                if verbose:
                    # Full: property name -> type
                    props = {}
                    for pname, ptype in raw_props.items():
                        props[pname] = ptype.get("type", str(ptype)) if isinstance(ptype, dict) else ptype

                    rels_out, rels_in = {}, {}
                    for rname, rinfo in raw_rels.items():
                        if not isinstance(rinfo, dict):
                            continue
                        targets = rinfo.get("labels", [])
                        if rinfo.get("direction", "out") == "out":
                            rels_out[rname] = targets
                        else:
                            rels_in[rname] = targets
                    node_info = {"count": count, "properties": props}
                    if rels_out:
                        node_info["outgoing"] = rels_out
                    if rels_in:
                        node_info["incoming"] = rels_in
                else:
                    # Compact: property names list, collapsed relationship types
                    prop_names = sorted(raw_props.keys())

                    # Collapse relationships: group targets per type+direction
                    out_rels = {}  # type -> set of target labels
                    in_rels = {}
                    for rname, rinfo in raw_rels.items():
                        if not isinstance(rinfo, dict):
                            continue
                        targets = rinfo.get("labels", [])
                        if rinfo.get("direction", "out") == "out":
                            out_rels.setdefault(rname, set()).update(targets)
                        else:
                            in_rels.setdefault(rname, set()).update(targets)

                    # Format as compact strings
                    rel_strs = []
                    for rname in sorted(out_rels):
                        targets = sorted(out_rels[rname])
                        # Skip listing targets if >4 (too noisy)
                        if len(targets) <= 4:
                            rel_strs.append(f"-[:{rname}]->({', '.join(targets)})")
                        else:
                            rel_strs.append(f"-[:{rname}]->({len(targets)} labels)")
                    for rname in sorted(in_rels):
                        sources = sorted(in_rels[rname])
                        if len(sources) <= 4:
                            rel_strs.append(f"<-[:{rname}]-({', '.join(sources)})")
                        else:
                            rel_strs.append(f"<-[:{rname}]-({len(sources)} labels)")

                    node_info = {"count": count, "properties": prop_names}
                    if rel_strs:
                        node_info["relationships"] = rel_strs

                nodes[key] = node_info

            elif schema_type == "relationship":
                raw_props = meta.get("properties", {})
                if verbose:
                    props = {}
                    for pname, ptype in raw_props.items():
                        props[pname] = ptype.get("type", str(ptype)) if isinstance(ptype, dict) else ptype
                    rel_types[key] = {"properties": props} if props else {}
                else:
                    rel_types[key] = sorted(raw_props.keys()) if raw_props else []

        # Sort nodes: primary labels (count > 10) first, then minor labels
        primary = {k: v for k, v in nodes.items() if v["count"] >= 10}
        minor = {k: v for k, v in nodes.items() if v["count"] < 10}

        if not verbose and minor:
            # Compact: collapse minor labels into a summary
            result["nodes"] = primary
            minor_summary = {k: v["count"] for k, v in sorted(minor.items(), key=lambda x: x[0])}
            result["minor_labels"] = minor_summary
        else:
            result["nodes"] = nodes

        result["relationship_types"] = rel_types

        # --- Indexes ---
        try:
            idx_records, _ = execute_read(
                "SHOW INDEXES YIELD name, type, labelsOrTypes, properties, state "
                "WHERE state = 'ONLINE' AND type <> 'LOOKUP' "
                "RETURN name, type, labelsOrTypes, properties",
                database=database, driver=driver
            )
            if verbose:
                result["indexes"] = [
                    {"name": r["name"], "type": r["type"],
                     "labels": r.get("labelsOrTypes"), "properties": r.get("properties")}
                    for r in idx_records
                ]
            else:
                result["indexes"] = [
                    f"{(r.get('labelsOrTypes') or ['?'])[0]}.{','.join(r.get('properties') or ['?'])} ({r['type'].lower()})"
                    for r in idx_records
                ]
        except Exception:
            result["indexes"] = []

        # Summary
        total_nodes = sum(n["count"] for n in nodes.values())
        result["summary"] = (
            f"{len(nodes)} labels, {total_nodes} nodes, "
            f"{len(rel_types)} rel types, {len(result.get('indexes', []))} indexes"
        )

    except Exception as e:
        import traceback
        result["error"] = f"Schema retrieval failed: {e}"
        result["traceback"] = traceback.format_exc()
        return result

    schema_cache[cache_key] = (time.time(), result)
    return result


if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    output(schema_info_impl(**params))
