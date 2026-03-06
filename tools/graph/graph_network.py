"""Show the full network around a person, organization, or entity.
---
description: Show N-hop network around any entity
databases: [corcoran]
read_only: true
---
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, GRAPH_DATABASE
from lib.io import setup_output, load_params, output


def graph_network_impl(name, depth=2, database=GRAPH_DATABASE, driver=None, **kwargs):
    """Core logic: N-hop network traversal around a named entity.

    Args:
        name: Entity name to search for (partial match)
        depth: Hops to traverse (1-3)
        database: Neo4j database (default: corcoran)
        driver: Optional shared Neo4j driver. Created if None.

    Returns:
        dict with targets_found, network edges, entity_count, entities
    """
    _driver = driver or get_neo4j_driver()
    result = {"query": name, "depth": depth, "database": database}

    try:
        with _driver.session(database=database) as session:
            # Find the target entity
            r = session.run(
                "MATCH (n) WHERE n.name CONTAINS $name "
                "RETURN n.name AS name, labels(n) AS labels, properties(n) AS props "
                "LIMIT 5",
                {"name": name}
            )
            targets = []
            for rec in r:
                props = dict(rec["props"])
                # Clean up large properties for display
                for key in ("bio", "content"):
                    if key in props and len(str(props.get(key, ""))) > 200:
                        props[key] = str(props[key])[:200] + "..."
                if "description" in props and len(str(props.get("description", ""))) > 300:
                    props["description"] = str(props["description"])[:300] + "..."
                targets.append({
                    "name": rec["name"],
                    "labels": rec["labels"],
                    "properties": props
                })
            result["targets_found"] = targets

            if targets:
                target_name = targets[0]["name"]

                # Get full network within N hops
                r = session.run(
                    "MATCH path = (start)-[*1.." + str(depth) + "]-(connected) "
                    "WHERE start.name = $name "
                    "WITH start, connected, relationships(path) AS rels "
                    "UNWIND rels AS r "
                    "WITH start, connected, r, startNode(r) AS rStart, endNode(r) AS rEnd "
                    "RETURN DISTINCT "
                    "  rStart.name AS from_name, labels(rStart)[0] AS from_type, "
                    "  type(r) AS rel_type, "
                    "  CASE WHEN r.role IS NOT NULL THEN r.role "
                    "       WHEN r.context IS NOT NULL THEN r.context "
                    "       WHEN r.relation IS NOT NULL THEN r.relation "
                    "       ELSE null END AS detail, "
                    "  r.startDate AS start_date, r.endDate AS end_date, "
                    "  rEnd.name AS to_name, labels(rEnd)[0] AS to_type "
                    "ORDER BY from_name, rel_type",
                    {"name": target_name}
                )
                edges = [dict(rec) for rec in r]
                result["network"] = edges
                result["edge_count"] = len(edges)

                # Unique entities in the network
                entities = set()
                for e in edges:
                    entities.add((e["from_name"], e["from_type"]))
                    entities.add((e["to_name"], e["to_type"]))
                result["entity_count"] = len(entities)
                result["entities"] = sorted(
                    [{"name": n, "type": t} for n, t in entities],
                    key=lambda x: (x["name"] or "")
                )

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
    r = graph_network_impl(
        name=p["name"],
        depth=p["depth"],
        database=p["database"],
    )
    output(r)
