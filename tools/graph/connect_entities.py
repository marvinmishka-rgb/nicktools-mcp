"""Wire a relationship between any two existing entities.
---
description: Wire a relationship between any two existing entities
creates_edges: [*]
databases: [corcoran]
---
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, GRAPH_DATABASE
from lib.io import setup_output, load_params, output
from lib.urls import VALID_PROVENANCE_TIERS, canonicalize_url

# connect_entities also accepts 'hearsay' as a provenance tier
CONNECT_PROVENANCE_TIERS = VALID_PROVENANCE_TIERS | {"hearsay"}


def connect_entities_impl(from_name, to_name, rel_type, properties=None,
                           database=GRAPH_DATABASE, driver=None, **kwargs):
    """Wire a relationship between two existing entities.

    Args:
        from_name: Source entity name
        to_name: Target entity name
        rel_type: Relationship type (e.g. EMPLOYED_BY, FAMILY_OF)
        properties: Dict of edge properties. Special keys:
            sourceUrl -- archived URL providing evidence
            provenanceTier -- one of: archived-verified, web-search, training-knowledge, hearsay
        database: Neo4j database (default: corcoran)
        driver: Optional shared Neo4j driver

    Returns:
        dict with wired, from, rel, to, properties, warnings
    """
    properties = dict(properties or {})
    _driver = driver or get_neo4j_driver()
    warnings = []

    try:
        with _driver.session(database=database) as session:
            # Find both entities
            q = "MATCH (n) WHERE n.name = $name RETURN n.name AS name, labels(n) AS labels LIMIT 1"
            from_rec = session.run(q, {"name": from_name}).single()
            to_rec = session.run(q, {"name": to_name}).single()

            if not from_rec:
                return {"error": f"Entity '{from_name}' not found"}
            if not to_rec:
                return {"error": f"Entity '{to_name}' not found"}

            # Extract and validate provenance properties
            provenance_tier = properties.pop("provenanceTier", None)
            source_url = properties.pop("sourceUrl", None)

            if provenance_tier and provenance_tier not in CONNECT_PROVENANCE_TIERS:
                warnings.append(
                    f"Invalid provenanceTier '{provenance_tier}'. "
                    f"Valid: {', '.join(sorted(CONNECT_PROVENANCE_TIERS))}. Defaulting to 'web-search'."
                )
                provenance_tier = "web-search"

            # Build and run MERGE
            set_parts = []
            params = {"fname": from_name, "tname": to_name}

            if source_url:
                source_url = canonicalize_url(source_url)
                set_parts.append("r.sourceUrl = $sourceUrl")
                params["sourceUrl"] = source_url
            if provenance_tier:
                set_parts.append("r.provenanceTier = $provenanceTier")
                params["provenanceTier"] = provenance_tier

            for k, v in properties.items():
                safe_k = k.replace("-", "_")
                set_parts.append(f"r.{safe_k} = ${safe_k}")
                params[safe_k] = v

            set_clause = f"SET {', '.join(set_parts)}" if set_parts else ""
            q = (
                f"MATCH (a {{name: $fname}}), (b {{name: $tname}}) "
                f"MERGE (a)-[r:{rel_type}]->(b) "
                f"{set_clause} "
                f"RETURN a.name AS `from`, type(r) AS rel, b.name AS `to`"
            )
            rec = session.run(q, params).single()

            if rec:
                result = {
                    "wired": True,
                    "from": rec["from"], "rel": rec["rel"], "to": rec["to"],
                    "properties": {**properties, **({"sourceUrl": source_url} if source_url else {}),
                                   **({"provenanceTier": provenance_tier} if provenance_tier else {})},
                }
                if warnings:
                    result["warnings"] = warnings
                return result
            else:
                return {"error": "Failed to wire relationship"}

    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}
    finally:
        if not driver:
            _driver.close()


if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = connect_entities_impl(
        from_name=p["from_name"],
        to_name=p["to_name"],
        rel_type=p["rel_type"],
        properties=p.get("properties", {}),
        database=p.get("database", GRAPH_DATABASE),
    )
    output(r)
