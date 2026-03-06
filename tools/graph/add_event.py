"""Create an Event node and wire it to involved people and organizations.
---
description: Create Event linking people/orgs to what happened
creates_nodes: [Event]
creates_edges: [INVOLVED_IN, OCCURRED_AT, SUPPORTED_BY]
databases: [corcoran]
---

Backward-compatible wrapper around node_ops + wire_evidence.
Preserves the original parameter signature while delegating to generic operations.
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, execute_write, execute_read, GRAPH_DATABASE
from lib.io import setup_output, load_params, output
from tools.graph.node_ops import node_impl
from tools.graph.wire_evidence import wire_evidence_impl

VALID_EVENT_TYPES = {"career-move", "development-milestone", "legal", "corporate",
                     "organizational", "other"}


def add_event_impl(name, event_type="other", date="", description="", source="",
                    involved=None, location="", extra_props=None, sources=None,
                    database=GRAPH_DATABASE, driver=None, **kwargs):
    """Create an Event node and wire it to involved people and organizations.

    Args:
        name: Event name (descriptive)
        event_type: career-move, development-milestone, legal, corporate, organizational, other
        date: When it happened (ISO: YYYY, YYYY-MM, or YYYY-MM-DD)
        description: What happened
        source: Lifestream entry ID that sourced this
        involved: List of {entity, role} dicts for participants
        location: Neighborhood name to wire OCCURRED_AT edge
        extra_props: Additional properties dict
        sources: List of {url, confidence, claim} for SUPPORTED_BY edges
        database: Neo4j database (default: corcoran)
        driver: Optional shared Neo4j driver

    Returns:
        dict with created, edges_wired, warnings
    """
    involved = involved or []
    extra_props = extra_props or {}
    sources = sources or []
    safe_type = event_type if event_type in VALID_EVENT_TYPES else "other"

    _driver = driver or get_neo4j_driver()
    result = {"created": False, "updated": False, "edges_wired": 0, "warnings": []}

    try:
        # 1. MERGE Event node via node_impl
        node_props = {"name": name, "type": safe_type}
        if date:
            node_props["date"] = date
        if description:
            node_props["description"] = description
        if source:
            node_props["source"] = source
        node_props.update({k: v for k, v in extra_props.items() if v is not None})

        node_result = node_impl("add", "Event", database=database, driver=_driver,
                                **node_props)
        if "error" in node_result:
            return node_result

        result["created"] = node_result.get("created", False)
        result["updated"] = node_result.get("updated", False)
        result["warnings"].extend(node_result.get("warnings", []))

        # 2. Wire involved entities
        for inv in involved:
            entity = inv.get("entity", "")
            role = inv.get("role", "")
            if not entity:
                continue

            # Check if entity exists; if not, create as Person (safe default)
            check_records, _ = execute_read(
                "MATCH (n {name: $name}) RETURN n LIMIT 1",
                database=database, driver=_driver, name=entity
            )
            if check_records:
                execute_write(
                    "MATCH (e:Event {name: $ename}), (n {name: $nname}) "
                    "MERGE (n)-[r:INVOLVED_IN]->(e) "
                    "SET r.role = $role, r.source = $source",
                    database=database, driver=_driver,
                    ename=name, nname=entity, role=role, source=source
                )
            else:
                execute_write(
                    "MATCH (e:Event {name: $ename}) "
                    "MERGE (n:Person {name: $nname}) "
                    "MERGE (n)-[r:INVOLVED_IN]->(e) "
                    "SET r.role = $role, r.source = $source",
                    database=database, driver=_driver,
                    ename=name, nname=entity, role=role, source=source
                )
            result["edges_wired"] += 1

        # 3. Wire location
        if location:
            execute_write(
                "MATCH (e:Event {name: $ename}) "
                "MERGE (n:Neighborhood {name: $loc}) "
                "MERGE (e)-[:OCCURRED_AT]->(n)",
                database=database, driver=_driver,
                ename=name, loc=location
            )
            result["edges_wired"] += 1

        # 4. Wire SUPPORTED_BY edges via wire_evidence
        if sources:
            ev_result = wire_evidence_impl(
                entity=name, sources=sources, label="Event",
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
    r = add_event_impl(
        name=p["name"], event_type=p.get("event_type", "other"),
        date=p.get("date", ""), description=p.get("description", ""),
        source=p.get("source", ""), involved=p.get("involved", []),
        location=p.get("location", ""), extra_props=p.get("extra_props", {}),
        sources=p.get("sources", []), database=p.get("database", GRAPH_DATABASE),
    )
    output(r)
