"""Unified graph write: create entities with relationships and sources in one call.
---
description: Write entities to the graph with nested relationships, sources, and labels
creates_nodes: [Person, Agent, Organization, Event, Document, Property]
creates_edges: [EMPLOYED_BY, WORKED_AT, AFFILIATED_WITH, FAMILY_OF, SUPPORTED_BY, *]
databases: [corcoran]
---

The recommended entry point for all graph writes. Accepts entities in natural
shapes -- nested relationships and sources are handled automatically.

Replaces: add_person, add_organization, add_event, add_document, add_property,
and multi-step commit sequences. All those tools still work but delegate here.

Usage via MCP:
    graph("write", {"entities": [
        {"label": "Person", "name": "Alice Chen", "description": "..."},
        {"label": "Organization", "name": "The Agency", "type": "Brokerage"},
        {"label": "Person", "name": "Bob Smith",
         "relationships": [
             {"type": "EMPLOYED_BY", "target": "The Agency",
              "target_label": "Organization", "props": {"role": "Agent"}}
         ],
         "sources": [{"url": "https://...", "confidence": "archived-verified"}]}
    ]})
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import GRAPH_DATABASE


def write_impl(entities=None, database=GRAPH_DATABASE, driver=None, **kwargs):
    """Write entities to the graph.

    Args:
        entities: List of entity dicts. Each must have:
            - label: Node label (Person, Organization, etc.)
            - merge key(s): e.g. 'name' for Person, ['address','city','state'] for Property
            Optional per entity:
            - relationships: [{type, target, target_label, props}]
            - sources: [{url, confidence, claim}]
            - extra_labels: ["Attorney", "Producer"]
            - Any other keys become node properties
        database: Neo4j database (default: corcoran)
        driver: Shared Neo4j driver

    Returns:
        dict with per-entity results, summary, and warnings
    """
    if not entities:
        return {"error": "Missing required parameter 'entities'. "
                         "Provide a list of entity dicts, each with at least 'label' and merge key(s)."}

    from lib.write_engine import write_entities
    return write_entities(entities, database=database, driver=driver)


if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = write_impl(**params)
    output(result)
