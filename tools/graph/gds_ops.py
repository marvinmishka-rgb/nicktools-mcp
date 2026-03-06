"""Graph Data Science operations: run algorithms on projected subgraphs.
---
description: Run GDS algorithms (PageRank, community detection, similarity, etc.) with managed projections
creates_nodes: []
creates_edges: []
databases: [corcoran]
---

Provides a managed interface to Neo4j GDS algorithms. Handles the full
projection lifecycle: create -> run algorithm -> return results -> drop projection.

Actions:
  run       -- Project subgraph, run algorithm, return stream results, drop projection
  list      -- List available GDS algorithms (stream variants only)
  estimate  -- Estimate memory requirements for an algorithm + projection
"""
import sys
import time
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import execute_read, execute_write, GRAPH_DATABASE


# In-memory cache for available GDS algorithms (lazy-loaded)
_gds_algorithms = None


def _discover_algorithms(driver=None, database=GRAPH_DATABASE):
    """Discover available GDS stream procedures via gds.list().

    Filters to stream variants only (no .write or .mutate -- those go
    through cypher passthrough). Excludes .estimate procedures.
    Caches results in memory for the server lifetime.
    """
    global _gds_algorithms
    if _gds_algorithms is not None:
        return _gds_algorithms

    records, _ = execute_read(
        "CALL gds.list() YIELD name, type "
        "WHERE type = 'procedure' AND name CONTAINS '.stream' "
        "AND NOT name CONTAINS '.estimate' "
        "RETURN name ORDER BY name",
        database=database, driver=driver,
    )

    algorithms = {}
    for record in records:
        full_name = record["name"]
        # Extract short name: "gds.pageRank.stream" -> "pageRank"
        parts = full_name.replace("gds.", "").replace(".stream", "").split(".")
        short_name = ".".join(parts)
        algorithms[short_name] = full_name

    _gds_algorithms = algorithms
    return algorithms


def _generate_projection_name(algorithm):
    """Generate a unique, timestamped projection name."""
    ts = int(time.time() * 1000)
    safe_algo = algorithm.replace(".", "-")
    return f"nicktools-{safe_algo}-{ts}"


def gds_impl(action, algorithm=None, nodes=None, relationships=None,
             config=None, projection_name=None, database=GRAPH_DATABASE,
             max_records=500, driver=None, **kwargs):
    """Run GDS algorithms with managed projection lifecycle.

    Args:
        action: "run", "list", or "estimate"
        algorithm: Algorithm short name (e.g. "pageRank", "louvain", "betweenness").
            Use action="list" to see available algorithms.
        nodes: Node labels to include in projection. String or list.
            E.g. "Agent" or ["Agent", "Person"]
        relationships: Relationship types for projection. String or list.
            E.g. "COLLABORATED_WITH" or ["COLLABORATED_WITH", "TEAM_MEMBER_OF"]
        config: Optional dict of algorithm-specific config.
            E.g. {"maxIterations": 20, "dampingFactor": 0.85} for pageRank
        projection_name: Optional custom projection name (auto-generated if omitted)
        database: Neo4j database (default: corcoran)
        max_records: Maximum results to return (default: 500)
        driver: Optional shared Neo4j driver

    Returns:
        For run: {algorithm, projection, records, record_count, dropped}
        For list: {algorithms: {shortName: fullProcedure, ...}, count}
        For estimate: {algorithm, required_memory, ...}
    """
    valid_actions = ("run", "list", "estimate")
    if not action or action not in valid_actions:
        return {"error": f"Invalid action '{action}'. Must be: {', '.join(valid_actions)}"}

    # --- List available algorithms ---
    if action == "list":
        algorithms = _discover_algorithms(driver=driver, database=database)
        return {
            "action": "list",
            "algorithms": algorithms,
            "count": len(algorithms),
            "hint": "Use the short name (left) as the 'algorithm' parameter."
        }

    # --- Validate algorithm ---
    if not algorithm:
        return {"error": "Missing 'algorithm' parameter. Use action='list' to see available algorithms."}

    algorithms = _discover_algorithms(driver=driver, database=database)
    if algorithm not in algorithms:
        # Try case-insensitive match
        lower_map = {k.lower(): k for k in algorithms}
        if algorithm.lower() in lower_map:
            algorithm = lower_map[algorithm.lower()]
        else:
            # Suggest close matches
            suggestions = [k for k in algorithms if algorithm.lower() in k.lower()]
            return {
                "error": f"Unknown algorithm '{algorithm}'.",
                "suggestions": suggestions[:10] if suggestions else [],
                "hint": "Use action='list' to see all available algorithms."
            }

    stream_proc = algorithms[algorithm]
    config = config or {}

    # --- Validate projection params ---
    if not nodes:
        return {"error": "Missing 'nodes' parameter. Provide node label(s) for the projection."}
    if not relationships:
        return {"error": "Missing 'relationships' parameter. Provide relationship type(s) for the projection."}

    # Normalize to lists
    if isinstance(nodes, str):
        nodes = [nodes]
    if isinstance(relationships, str):
        relationships = [relationships]

    proj_name = projection_name or _generate_projection_name(algorithm)

    # --- Estimate mode ---
    if action == "estimate":
        estimate_proc = stream_proc.replace(".stream", ".stream.estimate")
        try:
            records, _ = execute_read(
                f"CALL {estimate_proc}({{nodeProjection: $nodes, "
                f"relationshipProjection: $rels}}, $config) "
                f"YIELD requiredMemory, nodeCount, relationshipCount",
                database=database, driver=driver,
                nodes=nodes, rels=relationships, config=config,
            )
            if records:
                return {
                    "action": "estimate",
                    "algorithm": algorithm,
                    "required_memory": records[0]["requiredMemory"],
                    "node_count": records[0]["nodeCount"],
                    "relationship_count": records[0]["relationshipCount"],
                }
            return {"action": "estimate", "algorithm": algorithm, "result": "No estimate data returned"}
        except Exception as e:
            return {"error": f"Estimate failed: {e}", "algorithm": algorithm}

    # --- Run mode: project -> run -> collect -> drop ---
    result = {
        "action": "run",
        "algorithm": algorithm,
        "procedure": stream_proc,
        "projection": proj_name,
    }

    try:
        # 1. Create projection
        # Build node and relationship projection specs
        node_proj = nodes if len(nodes) > 1 else nodes[0]
        rel_proj = relationships if len(relationships) > 1 else relationships[0]

        project_cypher = (
            "CALL gds.graph.project($projName, $nodes, $rels) "
            "YIELD graphName, nodeCount, relationshipCount"
        )
        proj_records, _ = execute_write(
            project_cypher, database=database, driver=driver,
            projName=proj_name, nodes=node_proj, rels=rel_proj,
        )

        if proj_records:
            result["projection_nodes"] = proj_records[0]["nodeCount"]
            result["projection_rels"] = proj_records[0]["relationshipCount"]

        # 2. Run algorithm (stream variant)
        algo_cypher = (
            f"CALL {stream_proc}($projName, $config) "
            f"YIELD *"
        )
        algo_records, _ = execute_read(
            algo_cypher, database=database, driver=driver,
            projName=proj_name, config=config,
        )

        # 3. Collect results into plain dicts
        records_out = []
        for record in algo_records:
            row = {key: record[key] for key in record.keys()}
            records_out.append(row)

        # 4. Truncate before name resolution (don't resolve 4000 names)
        truncated = False
        if max_records > 0 and len(records_out) > max_records:
            records_out = records_out[:max_records]
            truncated = True

        # 5. Resolve nodeId -> name using gds.util.asNode()
        node_ids = set()
        for row in records_out:
            for key, val in row.items():
                if "nodeId" in key.lower() or key in ("nodeId", "sourceNodeId", "targetNodeId"):
                    if isinstance(val, (int, float)):
                        node_ids.add(int(val))

        if node_ids:
            try:
                id_list = list(node_ids)
                name_records, _ = execute_read(
                    "UNWIND $ids AS nid "
                    "RETURN nid, gds.util.asNode(nid).name AS name",
                    database=database, driver=driver,
                    ids=id_list,
                )
                id_to_name = {r["nid"]: r["name"] for r in name_records}

                # Enrich records with resolved names
                for row in records_out:
                    for key in list(row.keys()):
                        if key in ("nodeId", "sourceNodeId", "targetNodeId") and isinstance(row[key], (int, float)):
                            name = id_to_name.get(int(row[key]))
                            if name:
                                name_key = key.replace("Id", "Name") if "Id" in key else "nodeName"
                                row[name_key] = name
            except Exception:
                result.setdefault("warnings", []).append(
                    "Node name resolution failed -- results contain raw nodeIds only"
                )

        result["records"] = records_out
        result["record_count"] = len(records_out)
        result["truncated"] = truncated

    except Exception as e:
        import traceback
        result["error"] = f"GDS execution failed: {e}"
        result["traceback"] = traceback.format_exc()

    finally:
        # 4. Always drop projection (cleanup)
        try:
            execute_write(
                "CALL gds.graph.drop($projName, false) YIELD graphName",
                database=database, driver=driver,
                projName=proj_name,
            )
            result["dropped"] = True
        except Exception:
            result["dropped"] = False
            result.setdefault("warnings", []).append(
                f"Failed to drop projection '{proj_name}'. "
                f"Manual cleanup: CALL gds.graph.drop('{proj_name}')"
            )

    return result


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = gds_impl(**params)
    output(result)
