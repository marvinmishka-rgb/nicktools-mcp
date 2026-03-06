"""Create or update a Person node and wire all relationships.
---
description: Create/update Person with employment, affiliations, sources
creates_nodes: [Person]
creates_edges: [EMPLOYED_BY, WORKED_AT, AFFILIATED_WITH, FAMILY_OF, RESOLVES_TO, SUPPORTED_BY]
databases: [corcoran]
---

**DEPRECATED** -- Use graph("write", {entities: [{label: "Person", ...}]}) instead.

Thin wrapper that transforms domain-specific parameters into the unified write
format and delegates to write_engine. Preserves the original parameter signature
for backward compatibility.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, execute_write, GRAPH_DATABASE
from lib.io import setup_output, load_params, output


def add_person_impl(name, description="", source="", roles=None, employment=None,
                     affiliations=None, family=None, resolves_agent="",
                     extra_labels=None, extra_props=None, sources=None,
                     database=GRAPH_DATABASE, driver=None, **kwargs):
    """Create or update a Person node and wire all relationships.

    DEPRECATED: Use graph("write", {entities: [{label: "Person", ...}]}) instead.

    Args:
        name: Full name of the person
        description: Who they are
        source: Lifestream entry ID that sourced this
        roles: List of additional labels (e.g. ["Attorney", "Producer"])
        employment: List of {org, role, start, end, current} dicts
        affiliations: List of {org, role, start, end} dicts
        family: List of {name, relation, evidence} dicts
        resolves_agent: Agent name to link via RESOLVES_TO
        extra_labels: Additional node labels
        extra_props: Additional properties dict
        sources: List of {url, confidence, claim} for SUPPORTED_BY edges
        database: Neo4j database (default: corcoran)
        driver: Optional shared Neo4j driver

    Returns:
        dict with created, updated, edges_wired, warnings
    """
    roles = roles or []
    employment = employment or []
    affiliations = affiliations or []
    family = family or []
    extra_labels = extra_labels or []
    extra_props = extra_props or {}
    sources = sources or []

    # Build unified entity dict for write_engine
    entity = {"label": "Person", "name": name}
    if description:
        entity["description"] = description
    if source:
        entity["source"] = source
    entity.update({k: v for k, v in extra_props.items() if v is not None})

    # Transform employment -> generic relationships
    relationships = []
    for emp in employment:
        org_name = emp.get("org", "")
        if not org_name:
            continue
        rel_type = "EMPLOYED_BY" if emp.get("current", False) else "WORKED_AT"
        props = {}
        if emp.get("role"):
            props["role"] = emp["role"]
        if emp.get("start"):
            props["startDate"] = emp["start"]
        if emp.get("end"):
            props["endDate"] = emp["end"]
        if source:
            props["source"] = source
        if not emp.get("start") and not emp.get("end"):
            props["temporal"] = "unknown"
        relationships.append({
            "type": rel_type,
            "target": org_name,
            "target_label": "Organization",
            "props": props,
        })

    # Transform affiliations -> generic relationships
    for aff in affiliations:
        org_name = aff.get("org", "")
        if not org_name:
            continue
        props = {}
        if aff.get("role"):
            props["role"] = aff["role"]
        if aff.get("start"):
            props["startDate"] = aff["start"]
        if aff.get("end"):
            props["endDate"] = aff["end"]
        if source:
            props["source"] = source
        if not aff.get("start") and not aff.get("end"):
            props["temporal"] = "unknown"
        relationships.append({
            "type": "AFFILIATED_WITH",
            "target": org_name,
            "props": props,
        })

    # Transform family -> generic relationships
    for fam in family:
        other_name = fam.get("name", "")
        if not other_name:
            continue
        props = {"relation": fam.get("relation", "family")}
        if fam.get("evidence"):
            props["evidence"] = fam["evidence"]
        if source:
            props["source"] = source
        relationships.append({
            "type": "FAMILY_OF",
            "target": other_name,
            "target_label": "Person",
            "props": props,
        })

    if relationships:
        entity["relationships"] = relationships
    if sources:
        entity["sources"] = sources
    if roles or extra_labels:
        entity["extra_labels"] = roles + extra_labels

    # Delegate to write_engine
    from lib.write_engine import write_entities
    we_result = write_entities([entity], database=database, driver=driver)

    # Transform write_engine result to legacy format
    result = {"created": False, "updated": False, "edges_wired": 0, "warnings": []}
    if "error" in we_result:
        return we_result

    if we_result.get("entities"):
        e = we_result["entities"][0]
        result["created"] = e.get("created", False)
        result["updated"] = e.get("updated", False)
        result["edges_wired"] = e.get("relationships_wired", 0)
        if e.get("evidence_wired"):
            result["supported_by_wired"] = e["evidence_wired"]
            result["edges_wired"] += e["evidence_wired"]
    result["warnings"].extend(we_result.get("warnings", []))

    # Handle resolves_agent separately (fuzzy CONTAINS match -- not in write_engine)
    if resolves_agent:
        _driver = driver or get_neo4j_driver()
        try:
            records, _ = execute_write(
                "MATCH (p:Person {name: $pname}) "
                "MATCH (a:Agent) WHERE a.name CONTAINS $aname "
                "MERGE (a)-[r:RESOLVES_TO]->(p) "
                "SET r.confidence = 1.0, r.resolvedDate = date(), r.resolvedBy = $source "
                "RETURN a.name AS agent",
                database=database, driver=_driver,
                pname=name, aname=resolves_agent, source=source
            )
            agents = [rec["agent"] for rec in records]
            result["resolved_agents"] = agents
            result["edges_wired"] += len(agents)
        except Exception as e:
            result["warnings"].append(f"Agent resolution failed: {e}")
        finally:
            if not driver:
                _driver.close()

    return result


if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = add_person_impl(
        name=p["name"], description=p.get("description", ""),
        source=p.get("source", ""), roles=p.get("roles", []),
        employment=p.get("employment", []), affiliations=p.get("affiliations", []),
        family=p.get("family", []), resolves_agent=p.get("resolves_agent", ""),
        extra_labels=p.get("extra_labels", []), extra_props=p.get("extra_props", {}),
        sources=p.get("sources", []), database=p.get("database", GRAPH_DATABASE),
    )
    output(r)
