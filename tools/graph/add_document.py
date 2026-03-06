"""Create or update a Document node and wire all relationships.

Documents are primary records that ARE the evidence: patents, court filings,
leaked databases, corporate filings, government reports. Distinct from Source
nodes (web pages archived for provenance).

Phase 6 of tool-upgrade-plan-v3.md.
---
description: Create Document for primary records (filings, patents, leaked)
creates_nodes: [Document]
creates_edges: [MENTIONS, FILED_BY, SUPPORTED_BY]
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


VALID_DOC_TYPES = {
    "leaked-record", "court-filing", "corporate-filing", "patent",
    "tax-return", "government-report", "financial-disclosure",
    "property-record", "other",
}


def add_document_impl(title, doc_type="other", date="", author="",
                       description="", source="", archive_path="",
                       page_count=0, mentions=None, filed_by="",
                       extra_props=None, sources=None,
                       database=GRAPH_DATABASE, driver=None, **kwargs):
    """Create or update a Document node and wire all relationships.

    Args:
        title: Document title (used as MERGE key with doc_type)
        doc_type: One of VALID_DOC_TYPES
        date: Document date (ISO format)
        author: Author or issuing authority
        description: What the document contains/proves
        source: Lifestream entry ID that sourced this
        archive_path: Local path to archived copy
        page_count: Number of pages (for PDFs)
        mentions: List of {entity, page, context} dicts for MENTIONS edges
        filed_by: Person or org that filed/issued the document
        extra_props: Additional properties dict
        sources: List of {url, confidence, claim} for SUPPORTED_BY edges
        database: Neo4j database (default: corcoran)
        driver: Optional shared Neo4j driver

    Returns:
        dict with created, updated, edges_wired, warnings
    """
    mentions = mentions or []
    extra_props = extra_props or {}
    sources = sources or []

    if doc_type not in VALID_DOC_TYPES:
        doc_type = "other"

    _driver = driver or get_neo4j_driver()
    result = {"created": False, "updated": False, "edges_wired": 0, "warnings": []}

    try:
        # 1. MERGE Document node via node_impl
        # Document merge key is [name, docType] -- map title->name for DB property
        node_props = {"name": title, "docType": doc_type}
        if date:
            node_props["date"] = date
        if author:
            node_props["author"] = author
        if description:
            node_props["description"] = description
        if source:
            node_props["source"] = source
        if archive_path:
            node_props["archivePath"] = archive_path
        if page_count > 0:
            node_props["pageCount"] = page_count
        node_props.update({k: v for k, v in extra_props.items() if v is not None})

        node_result = node_impl("add", "Document", database=database, driver=_driver,
                                **node_props)
        if "error" in node_result:
            return node_result

        result["created"] = node_result.get("created", False)
        result["updated"] = node_result.get("updated", False)
        result["warnings"].extend(node_result.get("warnings", []))

        # 2. Wire MENTIONS edges
        for m in mentions:
            entity_name = m.get("entity", "")
            if not entity_name:
                continue
            page = m.get("page", 0)
            context = m.get("context", "")

            records, _ = execute_write(
                "MATCH (d:Document {name: $title, docType: $doc_type}) "
                "MATCH (n {name: $entity}) "
                "MERGE (d)-[r:MENTIONS]->(n) "
                "SET r.page = $page, r.context = $context, r.source = $source "
                "RETURN n.name AS matched",
                database=database, driver=_driver,
                title=title, doc_type=doc_type,
                entity=entity_name, page=page,
                context=context, source=source,
            )
            if records:
                result["edges_wired"] += 1
            else:
                result["warnings"].append(
                    f"Entity '{entity_name}' not found for MENTIONS edge. "
                    "Create it with add_person/add_organization first."
                )

        # 3. Wire FILED_BY edge
        if filed_by:
            records, _ = execute_write(
                "MATCH (d:Document {name: $title, docType: $doc_type}) "
                "MATCH (n {name: $filer}) "
                "MERGE (d)-[r:FILED_BY]->(n) "
                "SET r.source = $source "
                "RETURN n.name AS matched",
                database=database, driver=_driver,
                title=title, doc_type=doc_type,
                filer=filed_by, source=source,
            )
            if records:
                result["edges_wired"] += 1
            else:
                result["warnings"].append(
                    f"Filed-by entity '{filed_by}' not found. "
                    "Create it with add_person/add_organization first."
                )

        # 4. Wire SUPPORTED_BY edges via wire_evidence
        if sources:
            ev_result = wire_evidence_impl(
                entity=title, sources=sources, label="Document",
                # Document has composite key -- need custom match clause
                match_clause="MATCH (n:Document {name: $name, docType: $docType})",
                extra_params={"docType": doc_type},
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


if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = add_document_impl(
        title=p["title"], doc_type=p.get("doc_type", "other"),
        date=p.get("date", ""), author=p.get("author", ""),
        description=p.get("description", ""), source=p.get("source", ""),
        archive_path=p.get("archive_path", ""), page_count=p.get("page_count", 0),
        mentions=p.get("mentions", []), filed_by=p.get("filed_by", ""),
        extra_props=p.get("extra_props", {}), sources=p.get("sources", []),
        database=p.get("database", GRAPH_DATABASE),
    )
    output(r)
