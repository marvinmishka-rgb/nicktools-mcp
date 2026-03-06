"""
Layer 1 -- Pre-compiled read patterns for common graph queries.

Depends on: lib.db (Layer 0), lib.schema (Layer 0).

Replaces ad-hoc Cypher for the 80% of routine queries: entity lookup,
network traversal, and filtered search. Returns structured dicts ready
for JSON serialization.

Usage:
    from lib.read_patterns import read_entity, read_network, read_search

    # Entity details + immediate relationships
    read_entity("Alice Chen", label="Person", driver=driver)

    # N-hop network
    read_network("Alice Chen", depth=2, driver=driver)

    # Property search
    read_search(label="Person", where={"name_contains": "Chen"}, driver=driver)
"""
from lib.db import execute_read, GRAPH_DATABASE
from lib.schema import validate_label, get_merge_key, NODE_TYPES


def read_entity(name, label=None, include_sources=False,
                database=GRAPH_DATABASE, driver=None):
    """Read an entity's properties and immediate relationships.

    Args:
        name: Entity name to look up
        label: Optional label for MATCH specificity
        include_sources: If True, also return SUPPORTED_BY source URLs
        database: Neo4j database
        driver: Shared Neo4j driver

    Returns:
        dict with entity properties, relationships, and optionally sources
    """
    if label:
        ok, err = validate_label(label)
        if not ok:
            return {"error": err}
        match = f"MATCH (n:{label} {{name: $name}})"
    else:
        match = "MATCH (n {name: $name})"

    cypher = f"""
    {match}
    OPTIONAL MATCH (n)-[r]-(m)
    WHERE NOT type(r) IN ['suggestsLink']
    WITH n, r, m, labels(n) AS node_labels
    RETURN n, node_labels,
           collect(DISTINCT {{
               type: type(r),
               direction: CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END,
               target_name: coalesce(m.name, m.title, m.url, m.address, 'unnamed'),
               target_labels: labels(m),
               props: properties(r)
           }}) AS relationships
    """

    records, _ = execute_read(cypher, database=database, driver=driver, name=name)

    if not records:
        return {"found": False, "name": name, "label": label}

    rec = records[0]
    node = dict(rec["n"])
    result = {
        "found": True,
        "name": name,
        "labels": rec["node_labels"],
        "properties": node,
        "relationships": [dict(r) for r in rec["relationships"] if r.get("type")],
    }

    if include_sources:
        src_cypher = f"""
        {match}
        OPTIONAL MATCH (n)-[sb:SUPPORTED_BY]->(s:Source)
        RETURN s.url AS url, s.title AS title, s.archiveStatus AS status,
               sb.confidence AS confidence, sb.claim AS claim
        """
        src_records, _ = execute_read(src_cypher, database=database, driver=driver, name=name)
        result["sources"] = [
            {k: v for k, v in dict(r).items() if v is not None}
            for r in src_records if r["url"]
        ]

    return result


def read_network(name, depth=2, label=None, database=GRAPH_DATABASE, driver=None):
    """Read an entity's N-hop network.

    Returns all entities and relationships within N hops. Excludes
    suggestsLink edges and Source nodes to keep the network focused.

    Args:
        name: Starting entity name
        depth: Max hops (1-3, default 2)
        label: Optional label for MATCH specificity
        database: Neo4j database
        driver: Shared Neo4j driver

    Returns:
        dict with entities list and relationships list
    """
    depth = max(1, min(depth, 3))

    if label:
        ok, err = validate_label(label)
        if not ok:
            return {"error": err}
        match = f"MATCH (start:{label} {{name: $name}})"
    else:
        match = "MATCH (start {name: $name})"

    cypher = f"""
    {match}
    CALL apoc.path.subgraphAll(start, {{
        maxLevel: $depth,
        relationshipFilter: '>',
        labelFilter: '-Source'
    }}) YIELD nodes, relationships
    UNWIND nodes AS node
    WITH collect(DISTINCT {{
        name: coalesce(node.name, node.title, node.address, 'unnamed'),
        labels: labels(node)
    }}) AS entities, relationships
    UNWIND relationships AS r
    WITH entities, collect(DISTINCT {{
        type: type(r),
        from: coalesce(startNode(r).name, startNode(r).title, 'unnamed'),
        to: coalesce(endNode(r).name, endNode(r).title, 'unnamed'),
        props: properties(r)
    }}) AS rels
    RETURN entities, rels
    """

    try:
        records, _ = execute_read(cypher, database=database, driver=driver,
                                  name=name, depth=depth)
    except Exception:
        # APOC not available or other error -- fall back to simple path query
        return _read_network_fallback(name, depth, label, database, driver)

    if not records:
        return {"found": False, "name": name}

    rec = records[0]
    return {
        "found": True,
        "name": name,
        "depth": depth,
        "entities": [dict(e) for e in rec["entities"]],
        "relationships": [dict(r) for r in rec["rels"]],
        "entity_count": len(rec["entities"]),
        "relationship_count": len(rec["rels"]),
    }


def _read_network_fallback(name, depth, label, database, driver):
    """Simple path-based network query without APOC."""
    if label:
        match = f"MATCH (start:{label} {{name: $name}})"
    else:
        match = "MATCH (start {name: $name})"

    cypher = f"""
    {match}
    MATCH path = (start)-[*1..{depth}]-(connected)
    WHERE NOT connected:Source
    WITH DISTINCT connected, start
    RETURN collect(DISTINCT {{
        name: coalesce(connected.name, connected.title, connected.address, 'unnamed'),
        labels: labels(connected)
    }}) AS entities
    """

    records, _ = execute_read(cypher, database=database, driver=driver, name=name)

    if not records:
        return {"found": False, "name": name}

    return {
        "found": True,
        "name": name,
        "depth": depth,
        "entities": [dict(e) for e in records[0]["entities"]],
        "entity_count": len(records[0]["entities"]),
        "note": "Fallback query (no APOC) -- relationships not included"
    }


def read_search(label=None, where=None, limit=20, database=GRAPH_DATABASE, driver=None):
    """Search for entities by label and/or property filters.

    Args:
        label: Node label to search (e.g. "Person", "Organization")
        where: Dict of filter conditions. Supported keys:
            - name_contains: substring match on name (case-insensitive)
            - name_starts: prefix match on name
            - description_contains: substring match on description
            - any_prop: match any property containing this string
        limit: Max results (default 20, max 100)
        database: Neo4j database
        driver: Shared Neo4j driver

    Returns:
        dict with matches list and count
    """
    where = where or {}
    limit = max(1, min(limit, 100))

    if label:
        ok, err = validate_label(label)
        if not ok:
            return {"error": err}
        match = f"MATCH (n:{label})"
    else:
        match = "MATCH (n)"

    conditions = []
    params = {"limit": limit}

    if where.get("name_contains"):
        conditions.append("toLower(n.name) CONTAINS toLower($name_contains)")
        params["name_contains"] = where["name_contains"]

    if where.get("name_starts"):
        conditions.append("toLower(n.name) STARTS WITH toLower($name_starts)")
        params["name_starts"] = where["name_starts"]

    if where.get("description_contains"):
        conditions.append("toLower(n.description) CONTAINS toLower($desc_contains)")
        params["desc_contains"] = where["description_contains"]

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    cypher = f"""
    {match}
    {where_clause}
    RETURN n.name AS name, labels(n) AS labels,
           n.description AS description, n.addedDate AS addedDate
    ORDER BY n.name
    LIMIT $limit
    """

    records, _ = execute_read(cypher, database=database, driver=driver, **params)

    return {
        "matches": [
            {k: v for k, v in dict(r).items() if v is not None}
            for r in records
        ],
        "count": len(records),
        "label": label,
        "filters": where,
    }
