"""Update an existing lifestream entry's Neo4j node AND .md file in one call.
---
description: Update existing entry node and .md file
creates_nodes: [EntryRef]
creates_edges: [DISCUSSES]
databases: [lifestream, corcoran]
---
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, GRAPH_DATABASE, ENTRY_DATABASE
from lib.io import setup_output, load_params, output
from lib.paths import LIFESTREAM_DIR
from lib.entries import entry_path


def update_entry_impl(entry_id, title=None, entry_type=None, status=None,
                       content=None, domains=None, tags=None,
                       add_links=None, remove_links=None,
                       add_discusses=None, remove_discusses=None,
                       lifestream_dir=None, driver=None, **kwargs):
    """Update an existing lifestream entry's Neo4j node AND .md file.

    Only fields provided (not None) will be changed.

    Args:
        entry_id: The entry to update (e.g. 'ls-20260223-010')
        title: New title (or None to keep)
        entry_type: New type (or None to keep)
        status: New status (or None to keep)
        content: New content body (or None to keep)
        domains: List replacing domains (or None to keep)
        tags: List replacing tags (or None to keep)
        add_links: Dict of {rel_type: [target_ids]} to add
        remove_links: Dict of {rel_type: [target_ids]} to remove
        add_discusses: List of entity names to wire DISCUSSES edges to
        remove_discusses: List of entity names to remove DISCUSSES edges from
        lifestream_dir: Override for LIFESTREAM_DIR
        driver: Optional shared Neo4j driver

    Returns:
        dict with entry_id, changes, md_updated, md_path
    """
    add_links = add_links or {}
    remove_links = remove_links or {}
    add_discusses = add_discusses or []
    remove_discusses = remove_discusses or []
    stream_dir = Path(lifestream_dir) if lifestream_dir else LIFESTREAM_DIR

    _driver = driver or get_neo4j_driver()
    changes = []

    try:
        with _driver.session(database=ENTRY_DATABASE) as session:

            # 1. Verify entry exists and get current state
            result = session.run(
                "MATCH (s:StreamEntry {id: $id}) "
                "RETURN s.title AS title, s.type AS type, s.status AS status, "
                "       s.content AS content, s.domains AS domains, s.tags AS tags, "
                "       s.session AS session_date, s.timestamp AS timestamp",
                {"id": entry_id}
            )
            rec = result.single()
            if not rec:
                return {"error": f"Entry {entry_id} not found"}

            current = dict(rec)

            # 2. Build SET clause for Neo4j update
            set_parts = []
            params = {"id": entry_id}

            if title is not None:
                set_parts.append("s.title = $title")
                params["title"] = title
                changes.append("title updated")

            if entry_type is not None:
                set_parts.append("s.type = $type")
                params["type"] = entry_type
                changes.append(f"type: {current['type']} -> {entry_type}")

            if status is not None:
                set_parts.append("s.status = $status")
                params["status"] = status
                changes.append(f"status: {current['status']} -> {status}")

            if content is not None:
                set_parts.append("s.content = $content")
                params["content"] = content
                changes.append("content updated")

            if domains is not None:
                set_parts.append("s.domains = $domains")
                params["domains"] = domains
                changes.append(f"domains updated to {domains}")

            if tags is not None:
                set_parts.append("s.tags = $tags")
                params["tags"] = tags
                changes.append(f"tags updated to {tags}")

            if set_parts:
                cypher = f"MATCH (s:StreamEntry {{id: $id}}) SET {', '.join(set_parts)}"
                session.run(cypher, params)

            # 3. Re-wire domain/tag edges if changed
            if domains is not None:
                session.run(
                    "MATCH (s:StreamEntry {id: $id})-[r:inDomain]->() DELETE r",
                    {"id": entry_id}
                )
                for d in domains:
                    session.run(
                        "MATCH (s:StreamEntry {id: $id}) "
                        "MERGE (dom:Domain {name: $domain}) "
                        "MERGE (s)-[:inDomain]->(dom)",
                        {"id": entry_id, "domain": d}
                    )

            if tags is not None:
                session.run(
                    "MATCH (s:StreamEntry {id: $id})-[r:taggedWith]->() DELETE r",
                    {"id": entry_id}
                )
                for t in tags:
                    session.run(
                        "MATCH (s:StreamEntry {id: $id}) "
                        "MERGE (tag:Tag {name: $tag}) "
                        "MERGE (s)-[:taggedWith]->(tag)",
                        {"id": entry_id, "tag": t}
                    )

            # 4. Add new semantic edges
            for rel_type, targets in add_links.items():
                for target_id in targets:
                    if rel_type in ("connectsTo", "emergedFrom", "resolves"):
                        session.run(
                            f"MATCH (a:StreamEntry {{id: $from}}), (b:StreamEntry {{id: $to}}) "
                            f"MERGE (a)-[:{rel_type}]->(b)",
                            {"from": entry_id, "to": target_id}
                        )
                        changes.append(f"added {rel_type} -> {target_id}")

            # 5. Remove semantic edges
            for rel_type, targets in remove_links.items():
                for target_id in targets:
                    if rel_type in ("connectsTo", "emergedFrom", "resolves"):
                        session.run(
                            f"MATCH (a:StreamEntry {{id: $from}})-[r:{rel_type}]->(b:StreamEntry {{id: $to}}) "
                            f"DELETE r",
                            {"from": entry_id, "to": target_id}
                        )
                        changes.append(f"removed {rel_type} -> {target_id}")

            # 5b. Wire/unwire DISCUSSES edges in corcoran via EntryRef proxy nodes
            if add_discusses or remove_discusses:
                with _driver.session(database=GRAPH_DATABASE) as cor_session:
                    entry_title = title if title is not None else current["title"]

                    if add_discusses:
                        cor_session.run(
                            "MERGE (e:EntryRef {streamEntryId: $id}) "
                            "SET e.title = $title",
                            {"id": entry_id, "title": entry_title}
                        )
                        for entity_name in add_discusses:
                            rec2 = cor_session.run(
                                "MATCH (n) WHERE n.name = $name "
                                "RETURN n.name AS name LIMIT 1",
                                {"name": entity_name}
                            ).single()
                            if rec2:
                                cor_session.run(
                                    "MATCH (e:EntryRef {streamEntryId: $id}), (n {name: $name}) "
                                    "MERGE (e)-[r:DISCUSSES]->(n) "
                                    "SET r.streamEntryId = $id, r.createdAt = datetime()",
                                    {"id": entry_id, "name": entity_name}
                                )
                                changes.append(f"added DISCUSSES -> {entity_name}")
                            else:
                                changes.append(
                                    f"WARNING: Entity not found for DISCUSSES: '{entity_name}'"
                                )

                    for entity_name in remove_discusses:
                        cor_session.run(
                            "MATCH (e:EntryRef {streamEntryId: $id})-[r:DISCUSSES]->(n {name: $name}) "
                            "DELETE r",
                            {"id": entry_id, "name": entity_name}
                        )
                        changes.append(f"removed DISCUSSES -> {entity_name}")

        # 6. Update .md file frontmatter
        md_path = entry_path(entry_id, base_dir=stream_dir)
        md_updated = False

        if md_path.exists():
            md_text = md_path.read_text(encoding="utf-8")

            fm_match = re.match(r'^---\n(.*?)\n---\n?(.*)', md_text, re.DOTALL)
            if fm_match:
                fm_text = fm_match.group(1)
                body = fm_match.group(2)

                # Re-fetch the updated node to build fresh frontmatter
                with _driver.session(database=ENTRY_DATABASE) as session:
                    result = session.run(
                        "MATCH (s:StreamEntry {id: $id}) "
                        "RETURN s.title AS title, s.type AS type, s.status AS status, "
                        "       s.content AS content, s.domains AS domains, s.tags AS tags, "
                        "       s.session AS session_date",
                        {"id": entry_id}
                    )
                    updated = dict(result.single())

                    edge_result = session.run(
                        "MATCH (s:StreamEntry {id: $id})-[r:connectsTo|emergedFrom|resolves]->(t:StreamEntry) "
                        "RETURN type(r) AS rel, collect(t.id) AS targets",
                        {"id": entry_id}
                    )
                    edges = {rec3["rel"]: rec3["targets"] for rec3 in edge_result}

                # Build new frontmatter
                domains_yaml = "\n".join(f"  - {d}" for d in (updated["domains"] or []))
                tags_yaml = "\n".join(f"  - {t}" for t in (updated["tags"] or []))

                # Preserve timestamp from original frontmatter
                ts_match = re.search(r'timestamp:\s*"([^"]*)"', fm_text)
                timestamp = ts_match.group(1) if ts_match else ""

                new_fm = f"""id: {entry_id}
type: {updated['type']}
title: "{updated['title']}"
timestamp: "{timestamp}"
session: "{updated['session_date']}"
domains:
{domains_yaml}
tags:
{tags_yaml}
status: {updated['status']}"""

                if edges:
                    links_parts = []
                    for rel_type, targets in edges.items():
                        links_parts.append(f"  {rel_type}: [{', '.join(targets)}]")
                    new_fm += "\nlinks:\n" + "\n".join(links_parts)

                # If content was updated, replace the body too
                if content is not None:
                    body = f"\n# {updated['title']}\n\n{content}\n"
                elif title is not None:
                    body = re.sub(r'^\n?#\s+.*$', f"\n# {title}", body, count=1, flags=re.MULTILINE)

                new_md = f"---\n{new_fm}\n---{body}"
                md_path.write_text(new_md, encoding="utf-8")
                md_updated = True
                changes.append("md file updated")
        else:
            changes.append(f"WARNING: md file not found at {md_path}")

        return {
            "entry_id": entry_id,
            "changes": changes,
            "md_updated": md_updated,
            "md_path": str(md_path) if md_updated else None,
        }

    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}
    finally:
        if not driver:
            _driver.close()


# Subprocess entry point (backward compat with server.py dispatcher)
if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = update_entry_impl(
        entry_id=p["entry_id"],
        title=p.get("title"), entry_type=p.get("entry_type"),
        status=p.get("status"), content=p.get("content"),
        domains=p.get("domains"), tags=p.get("tags"),
        add_links=p.get("add_links", {}),
        remove_links=p.get("remove_links", {}),
        add_discusses=p.get("add_discusses", []),
        remove_discusses=p.get("remove_discusses", []),
        lifestream_dir=p.get("lifestream_dir"),
    )
    output(r)
