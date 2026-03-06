"""Create a complete lifestream entry: .md file + Neo4j node + all edges.
---
description: Create lifestream entry with .md file + Neo4j node + all edges
creates_nodes: [StreamEntry, EntryRef]
creates_edges: [CITES, DISCUSSES, followedBy, PRODUCED_IN]
databases: [lifestream, corcoran]
---
"""
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, GRAPH_DATABASE, ENTRY_DATABASE
from lib.io import setup_output, load_params, output
from lib.paths import LIFESTREAM_DIR, ensure_dir
from lib.entries import normalize_path, next_entry_id
from lib.sources import wire_cites_edges


def create_entry_impl(title, entry_type="finding", content="", domains=None,
                       tags=None, links=None, sources=None, discusses=None,
                       phase=None, status="active", lifestream_dir=None,
                       driver=None, **kwargs):
    """Create a complete lifestream entry: .md file + Neo4j node + all edges.

    Args:
        title: Entry title
        entry_type: One of the valid entry types
        content: Full text content for the entry
        domains: List of domain names
        tags: List of tag names
        links: Dict of {rel_type: [target_ids]} for semantic edges
        sources: List of {url, confidence, claim} for CITES edges
        discusses: List of entity names for DISCUSSES edges in corcoran
        phase: Phase ID string ("project/number", e.g. "nicktools-1.0/4f")
               to auto-wire a DOCUMENTED_BY edge from this entry to the phase
        status: Entry status (default: active)
        lifestream_dir: Override for LIFESTREAM_DIR
        driver: Optional shared Neo4j driver

    Returns:
        dict with entry_id, md_file, domains, tags, links_wired,
        sources_wired, discusses_wired, phase_linked, status, warnings
    """
    domains = domains or []
    tags = tags or []
    links = links or {}
    sources = sources or []
    discusses = discusses or []
    stream_dir = Path(lifestream_dir) if lifestream_dir else LIFESTREAM_DIR

    _driver = driver or get_neo4j_driver()
    result = {"status": "complete", "warnings": []}
    cowork_session_title = None

    try:
        # Step 1: Determine next entry ID
        with _driver.session(database=ENTRY_DATABASE) as session:
            entry_id, session_date = next_entry_id(session)

        # Step 2: Write .md file
        date_dir = stream_dir / datetime.now().strftime("%Y/%m/%d")
        ensure_dir(date_dir, "lifestream entry directory")
        md_path = date_dir / f"{entry_id}.md"

        # Build YAML frontmatter
        domains_yaml = "\n".join(f"  - {d}" for d in domains)
        tags_yaml = "\n".join(f"  - {t}" for t in tags)
        links_yaml = ""
        for rel_type, targets in links.items():
            if targets:
                links_yaml += f"  {rel_type}: [{', '.join(targets)}]\n"

        frontmatter = f"""---
id: {entry_id}
type: {entry_type}
title: "{title}"
timestamp: "{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
session: "{session_date}"
domains:
{domains_yaml}
tags:
{tags_yaml}
status: {status}"""

        if links_yaml:
            frontmatter += f"""
links:
{links_yaml.rstrip()}"""

        frontmatter += """
---"""

        md_content = f"""{frontmatter}

# {title}

{content}
"""
        md_path.write_text(md_content, encoding='utf-8')

        # Step 3: Create Neo4j node
        with _driver.session(database=ENTRY_DATABASE) as session:
            session.run(
                """CREATE (s:StreamEntry {
                    id: $id,
                    type: $type,
                    title: $title,
                    timestamp: datetime(),
                    session: $session,
                    domains: $domains,
                    tags: $tags,
                    status: $status,
                    content: $content
                })""",
                {
                    "id": entry_id,
                    "type": entry_type,
                    "title": title,
                    "session": session_date,
                    "domains": domains,
                    "tags": tags,
                    "status": status,
                    "content": content,
                }
            )

            # Step 4: Wire semantic edges
            for rel_type, targets in links.items():
                for target_id in targets:
                    if rel_type in ("connectsTo", "emergedFrom", "resolves"):
                        session.run(
                            f"MATCH (a:StreamEntry {{id: $from}}), (b:StreamEntry {{id: $to}}) "
                            f"MERGE (a)-[:{rel_type}]->(b)",
                            {"from": entry_id, "to": target_id}
                        )

            # Step 5: Create File node and createdFile edge
            rel_md_path = normalize_path(md_path)
            session.run(
                """MERGE (f:File {path: $path})
                SET f.filename = $filename, f.fileType = 'md', f.created = date($date)
                WITH f
                MATCH (s:StreamEntry {id: $id})
                MERGE (s)-[:createdFile]->(f)""",
                {
                    "path": rel_md_path,
                    "filename": f"{entry_id}.md",
                    "date": session_date,
                    "id": entry_id,
                }
            )

            # Step 6: Wire CITES edges from sources
            sources_wired, cites_warnings = wire_cites_edges(entry_id, sources, session=session)
            result["warnings"].extend(cites_warnings)

            # Step 6b: Auto-link to current CoworkSession via PRODUCED_IN
            # Strategy: use cached session detection (fast, reliable) with
            # timestamp-window fallback for edge cases.
            try:
                from lib.session_detect import get_cached_session
                cached = get_cached_session()
                if cached and cached.get("sessionId"):
                    # Direct match via cached session -- no timing window needed
                    link_result = session.run("""
                        MATCH (se:StreamEntry {id: $id})
                        MATCH (cs:CoworkSession {sessionId: $sessionId})
                        MERGE (se)-[r:PRODUCED_IN]->(cs)
                        ON CREATE SET r.linkedBy = 'create_entry_cached', r.linkedAt = datetime()
                        RETURN cs.title AS sessionTitle
                    """, {"id": entry_id, "sessionId": cached["sessionId"]})
                    rec = link_result.single()
                    if rec:
                        cowork_session_title = rec["sessionTitle"]
                else:
                    # Fallback: timestamp-window matching (original approach)
                    link_result = session.run("""
                        MATCH (se:StreamEntry {id: $id})
                        WHERE se.timestamp IS NOT NULL
                        WITH se, se.timestamp AS entryTime
                        MATCH (cs:CoworkSession)
                        WHERE cs.createdAt IS NOT NULL AND cs.lastAuditTimestamp IS NOT NULL
                        AND entryTime >= cs.createdAt
                        AND entryTime <= cs.lastAuditTimestamp + duration('PT10M')
                        WITH se, cs ORDER BY cs.createdAt DESC LIMIT 1
                        MERGE (se)-[r:PRODUCED_IN]->(cs)
                        ON CREATE SET r.linkedBy = 'create_entry', r.linkedAt = datetime()
                        RETURN cs.title AS sessionTitle
                    """, {"id": entry_id})
                    rec = link_result.single()
                    if rec:
                        cowork_session_title = rec["sessionTitle"]
            except Exception:
                pass  # Non-critical -- session_ingest backfill handles gaps

            # Step 6c: Auto-link to Phase via DOCUMENTED_BY
            phase_linked = None
            if phase:
                try:
                    phase_rec = session.run("""
                        MATCH (se:StreamEntry {id: $id})
                        MATCH (p:Phase {phaseId: $phaseId})
                        MERGE (se)-[r:DOCUMENTED_BY]->(p)
                        ON CREATE SET r.createdAt = datetime()
                        RETURN p.title AS phaseTitle, p.phaseNumber AS phaseNumber
                    """, {"id": entry_id, "phaseId": phase}).single()
                    if phase_rec:
                        phase_linked = f"{phase_rec['phaseNumber']}: {phase_rec['phaseTitle']}"
                    else:
                        result["warnings"].append(
                            f"Phase not found: '{phase}'"
                        )
                except Exception:
                    pass  # Non-critical

        # Step 7: Wire DISCUSSES edges in corcoran via EntryRef proxy nodes
        discusses_wired = 0
        if discusses:
            with _driver.session(database=GRAPH_DATABASE) as cor_session:
                # Create or update EntryRef proxy node
                cor_session.run(
                    "MERGE (e:EntryRef {streamEntryId: $id}) "
                    "SET e.title = $title",
                    {"id": entry_id, "title": title}
                )
                # Wire DISCUSSES edge to each named entity
                for entity_name in discusses:
                    rec = cor_session.run(
                        "MATCH (n) WHERE n.name = $name "
                        "RETURN n.name AS name, labels(n) AS labels LIMIT 1",
                        {"name": entity_name}
                    ).single()
                    if rec:
                        cor_session.run(
                            "MATCH (e:EntryRef {streamEntryId: $id}), (n {name: $name}) "
                            "MERGE (e)-[r:DISCUSSES]->(n) "
                            "SET r.streamEntryId = $id, r.createdAt = datetime()",
                            {"id": entry_id, "name": entity_name}
                        )
                        discusses_wired += 1
                    else:
                        result["warnings"].append(
                            f"Entity not found in corcoran: '{entity_name}'"
                        )

        # Step 7b: Auto-detect DISCUSSES entities if none were explicitly provided
        auto_detected = []
        if not discusses and (title or content):
            try:
                from lib.entity_match import find_entities
                scan_text = f"{title}\n{content}" if content else title
                detected = find_entities(scan_text, driver=_driver)
                if detected:
                    with _driver.session(database=GRAPH_DATABASE) as cor_session:
                        # Create EntryRef proxy node
                        cor_session.run(
                            "MERGE (e:EntryRef {streamEntryId: $id}) "
                            "SET e.title = $title",
                            {"id": entry_id, "title": title}
                        )
                        for entity in detected:
                            cor_session.run(
                                "MATCH (e:EntryRef {streamEntryId: $id}), (n {name: $name}) "
                                "MERGE (e)-[r:DISCUSSES]->(n) "
                                "SET r.streamEntryId = $id, r.createdAt = datetime(), "
                                "    r.autoDetected = true",
                                {"id": entry_id, "name": entity["name"]}
                            )
                            discusses_wired += 1
                            auto_detected.append(entity["name"])
            except Exception:
                pass  # Non-critical -- manual wiring still works

        # Provenance warning
        if entry_type in ("finding", "analysis") and not sources:
            result["warnings"].append(
                "WARNING: Finding/analysis created with no sources. Consider adding provenance."
            )

        # Build result
        result.update({
            "entry_id": entry_id,
            "md_file": str(md_path),
            "neo4j": "node + edges created",
            "domains": domains,
            "tags": tags,
            "links_wired": {k: len(v) for k, v in links.items() if v},
            "sources_wired": sources_wired,
            "discusses_wired": discusses_wired,
            "auto_detected_entities": auto_detected,
            "phase_linked": phase_linked,
            "cowork_session": cowork_session_title,
        })

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
    r = create_entry_impl(
        title=p["title"], entry_type=p.get("entry_type", "finding"),
        content=p.get("content", ""), domains=p.get("domains", []),
        tags=p.get("tags", []), links=p.get("links", {}),
        sources=p.get("sources", []), discusses=p.get("discusses", []),
        phase=p.get("phase"), status=p.get("status", "active"),
        lifestream_dir=p.get("lifestream_dir"),
    )
    output(r)
