"""Unified graph read: entity lookup, network traversal, and filtered search.
---
description: Read entities, explore networks, and search the graph with pre-compiled patterns
creates_nodes: []
creates_edges: []
databases: [corcoran]
---

The recommended entry point for routine graph reads. Three modes:

  entity  -- Single entity details + immediate relationships + optional sources
  network -- N-hop network traversal (1-3 hops, excludes Source nodes)
  search  -- Filtered entity search by label and property conditions

Replaces ad-hoc Cypher for ~80% of routine read queries.

Usage via MCP:
    graph("read", {"entity": "Alice Chen", "label": "Person", "include_sources": true})
    graph("read", {"entity": "Alice Chen", "network": 2})
    graph("read", {"label": "Person", "where": {"name_contains": "Chen"}, "limit": 20})
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import GRAPH_DATABASE


def read_impl(entity=None, network=None, label=None, where=None,
              include_sources=False, limit=20, database=GRAPH_DATABASE,
              driver=None, **kwargs):
    """Read from the graph using pre-compiled patterns.

    Three modes (detected from parameters):
      1. Entity mode: entity= provided, network= not set
         -> Returns entity properties + immediate relationships
      2. Network mode: entity= provided, network= set (1-3)
         -> Returns N-hop network around entity
      3. Search mode: entity= not provided
         -> Returns entities matching label + where filters

    Args:
        entity: Entity name for lookup or network center
        network: Hop depth for network mode (1-3). Triggers network mode.
        label: Node label for MATCH specificity or search filter
        where: Dict of search filters (name_contains, name_starts, description_contains)
        include_sources: If True, include SUPPORTED_BY sources (entity mode only)
        limit: Max results for search mode (default 20, max 100)
        database: Neo4j database
        driver: Shared Neo4j driver

    Returns:
        dict with results appropriate to the mode
    """
    from lib.read_patterns import read_entity, read_network, read_search

    if entity and network:
        # Network mode
        return read_network(entity, depth=int(network), label=label,
                           database=database, driver=driver)

    if entity:
        # Entity mode
        return read_entity(entity, label=label, include_sources=include_sources,
                          database=database, driver=driver)

    # Search mode
    return read_search(label=label, where=where, limit=limit,
                      database=database, driver=driver)


if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = read_impl(**params)
    output(result)
