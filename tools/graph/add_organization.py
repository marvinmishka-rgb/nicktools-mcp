"""Create or update an Organization node and wire relationships.
---
description: Create/update Organization with typed relationships
creates_nodes: [Organization]
creates_edges: [COLLABORATED_WITH, PART_OF, INVOLVED_IN, SUPPORTED_BY]
databases: [corcoran]
---

Backward-compatible wrapper around node_ops + wire_evidence.
Preserves the original parameter signature while delegating to generic operations.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, execute_write, GRAPH_DATABASE
from lib.io import setup_output, load_params, output
from tools.graph.node_ops import node_impl
from tools.graph.wire_evidence import wire_evidence_impl

VALID_ORG_TYPES = {"developer", "brokerage", "publisher", "law-firm", "political",
                   "faith", "educational", "government", "nonprofit", "other"}

ALLOWED_REL_TYPES = {"COLLABORATED_WITH", "PART_OF", "INVOLVED_IN"}


def add_organization_impl(name, org_type="other", description="", source="",
                           relationships=None, extra_props=None, sources=None,
                           database=GRAPH_DATABASE, driver=None, **kwargs):
    """Create or update an Organization node and wire relationships.

    Args:
        name: Organization name
        org_type: Type (developer, brokerage, publisher, law-firm, political,
                  faith, educational, government, nonprofit, other)
        description: What the organization is/does
        source: Lifestream entry ID that sourced this
        relationships: List of {entity, rel, role, start, end, context} dicts
        extra_props: Additional properties dict
        sources: List of {url, confidence, claim} for SUPPORTED_BY edges
        database: Neo4j database (default: corcoran)
        driver: Optional shared Neo4j driver

    Returns:
        dict with created, edges_wired, warnings
    """
    relationships = relationships or []
    extra_props = extra_props or {}
    sources = sources or []
    safe_type = org_type if org_type in VALID_ORG_TYPES else "other"

    _driver = driver or get_neo4j_driver()
    result = {"created": False, "updated": False, "edges_wired": 0, "warnings": []}

    try:
        # 1. MERGE Organization node via node_impl
        # Filter empty strings to prevent overwriting existing values (matches COALESCE behavior)
        node_props = {"name": name, "type": safe_type}
        if description:
            node_props["description"] = description
        if source:
            node_props["source"] = source
        node_props.update({k: v for k, v in extra_props.items() if v is not None})

        node_result = node_impl("add", "Organization", database=database, driver=_driver,
                                **node_props)
        if "error" in node_result:
            return node_result

        result["created"] = node_result.get("created", False)
        result["updated"] = node_result.get("updated", False)
        result["warnings"].extend(node_result.get("warnings", []))

        # 2. Wire relationship edges
        for rel in relationships:
            entity = rel.get("entity", "")
            rel_type = rel.get("rel", "COLLABORATED_WITH")
            role = rel.get("role", "")
            start = rel.get("start", "")
            end = rel.get("end", "")
            context = rel.get("context", "")

            if rel_type not in ALLOWED_REL_TYPES:
                result["warnings"].append(
                    f"Skipped {rel_type} for {entity} -- use add_person for employment edges"
                )
                continue

            q = (
                f"MATCH (o:Organization {{name: $oname}}) "
                f"MERGE (t {{name: $tname}}) "
                f"MERGE (o)-[r:{rel_type}]->(t) "
                f"SET r.source = $source"
            )
            params = {"oname": name, "tname": entity, "source": source}
            if role:
                q += ", r.role = $role"
                params["role"] = role
            if start:
                q += ", r.startDate = $start"
                params["start"] = start
            if end:
                q += ", r.endDate = $end"
                params["end"] = end
            if context:
                q += ", r.context = $context"
                params["context"] = context

            execute_write(q, database=database, driver=_driver, **params)
            result["edges_wired"] += 1

        # 3. Wire SUPPORTED_BY edges via wire_evidence
        if sources:
            ev_result = wire_evidence_impl(
                entity=name, sources=sources, label="Organization",
                database=database, driver=_driver
            )
            if "error" not in ev_result:
                result["edges_wired"] += ev_result.get("edges_wired", 0)
                result["supported_by_wired"] = ev_result.get("edges_wired", 0)
                result["warnings"].extend(ev_result.get("warnings", []))
            else:
                result["warnings"].append(f"Evidence wiring failed: {ev_result['error']}")

    except Exception as e:
        result["error"] = str(e)
        import traceback
        result["traceback"] = traceback.format_exc()
    finally:
        if not driver:
            _driver.close()

    return result


# Subprocess entry point (backward compat with server.py dispatcher)
if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = add_organization_impl(
        name=p["name"], org_type=p.get("org_type", "other"),
        description=p.get("description", ""), source=p.get("source", ""),
        relationships=p.get("relationships", []),
        extra_props=p.get("extra_props", {}),
        sources=p.get("sources", []), database=p.get("database", GRAPH_DATABASE),
    )
    output(r)
