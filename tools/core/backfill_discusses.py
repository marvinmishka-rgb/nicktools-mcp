"""Retroactively wire DISCUSSES edges for entries missing them.
---
description: Backfill DISCUSSES edges using entity name matching
creates_nodes: [EntryRef]
creates_edges: [DISCUSSES]
databases: [lifestream, corcoran]
---

Scans StreamEntry nodes that have no EntryRef in corcoran, runs entity
name matching against title + content, and wires DISCUSSES edges for
any matches found. Safe to run repeatedly -- uses MERGE for idempotency.
"""
import sys
import re
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))

from lib.db import get_neo4j_driver, GRAPH_DATABASE, ENTRY_DATABASE
from lib.io import setup_output, load_params, output
from lib.entity_match import find_entities
from lib.entries import entry_path


def _read_md_content(entry_id):
    """Read content from the .md file, stripping YAML frontmatter.

    Returns the body text after the closing '---', or empty string on failure.
    """
    try:
        md_path = entry_path(entry_id)
        if not md_path.exists():
            return ""
        text = md_path.read_text(encoding="utf-8")
        # Strip YAML frontmatter (between first and second ---)
        match = re.match(r'^---\s*\n.*?\n---\s*\n', text, re.DOTALL)
        if match:
            return text[match.end():].strip()
        return text.strip()
    except Exception:
        return ""


def backfill_discusses_impl(batch_size=50, dry_run=False, entry_filter=None,
                             driver=None, **kwargs):
    """Backfill DISCUSSES edges for entries missing them.

    Args:
        batch_size: Max entries to process (default 50)
        dry_run: If True, report what would be wired without making changes
        entry_filter: Optional prefix filter for entry IDs (e.g. 'ls-20260228')
        driver: Optional shared Neo4j driver

    Returns:
        dict with entries_processed, entries_matched, edges_created, details
    """
    _driver = driver or get_neo4j_driver()

    try:
        # Find entries without EntryRef in corcoran
        with _driver.session(database=GRAPH_DATABASE) as cor_session:
            existing_refs = cor_session.run(
                "MATCH (e:EntryRef) RETURN e.streamEntryId AS id"
            )
            existing_ids = {r["id"] for r in existing_refs}

        # Get entries from lifestream that need backfilling
        with _driver.session(database=ENTRY_DATABASE) as ls_session:
            filter_clause = ""
            params = {}
            if entry_filter:
                filter_clause = "AND s.id STARTS WITH $prefix"
                params["prefix"] = entry_filter

            result = ls_session.run(
                f"MATCH (s:StreamEntry) "
                f"WHERE s.title IS NOT NULL {filter_clause} "
                f"RETURN s.id AS id, s.title AS title, s.content AS content "
                f"ORDER BY s.id DESC",
                params
            )
            all_entries = [dict(r) for r in result]

        # Filter to entries without existing EntryRef
        candidates = [e for e in all_entries if e["id"] not in existing_ids]
        to_process = candidates[:batch_size]

        # Process each entry
        entries_processed = 0
        entries_matched = 0
        total_edges = 0
        details = []

        md_fallback_count = 0
        for entry in to_process:
            entry_id = entry["id"]
            title = entry["title"] or ""
            content = entry["content"] or ""

            # Fallback: read .md file if Neo4j content is empty
            if not content.strip():
                content = _read_md_content(entry_id)
                if content:
                    md_fallback_count += 1

            scan_text = f"{title}\n{content}" if content else title

            matches = find_entities(scan_text, driver=_driver)
            entries_processed += 1

            if not matches:
                continue

            entries_matched += 1
            entity_names = [m["name"] for m in matches]

            if not dry_run:
                with _driver.session(database=GRAPH_DATABASE) as cor_session:
                    # Create EntryRef
                    cor_session.run(
                        "MERGE (e:EntryRef {streamEntryId: $id}) "
                        "SET e.title = $title",
                        {"id": entry_id, "title": title}
                    )
                    # Wire DISCUSSES edges
                    for name in entity_names:
                        cor_session.run(
                            "MATCH (e:EntryRef {streamEntryId: $id}), (n {name: $name}) "
                            "MERGE (e)-[r:DISCUSSES]->(n) "
                            "SET r.streamEntryId = $id, r.createdAt = datetime(), "
                            "    r.autoDetected = true, r.backfilled = true",
                            {"id": entry_id, "name": name}
                        )
                        total_edges += 1
            else:
                total_edges += len(entity_names)

            details.append({
                "entry_id": entry_id,
                "title": title[:80],
                "entities_matched": len(entity_names),
                "entities": entity_names[:10],  # Cap at 10 for readability
            })

        return {
            "dry_run": dry_run,
            "entries_scanned": len(to_process),
            "entries_without_ref": len(candidates),
            "entries_processed": entries_processed,
            "entries_matched": entries_matched,
            "edges_created": total_edges,
            "md_fallback_used": md_fallback_count,
            "details": details,
        }

    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}
    finally:
        if not driver:
            _driver.close()


if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = backfill_discusses_impl(**params)
    output(result)
