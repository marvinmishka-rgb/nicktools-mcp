"""Bootstrap a new session with full context from the lifestream graph.
---
description: Bootstrap session with context from lifestream graph
databases: [lifestream, corcoran]
read_only: true
---
"""
import sys
from datetime import datetime

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, GRAPH_DATABASE, ENTRY_DATABASE
from lib.io import setup_output, load_params, output


def session_start_impl(focus_domain=None, topic=None, driver=None, **kwargs):
    """Bootstrap a new session with full context from the lifestream graph.

    Args:
        focus_domain: Optional domain to emphasize (e.g. 'corcoran', 'tooling')
        topic: Optional topic for full-text search
        driver: Optional shared Neo4j driver

    Returns:
        dict with generated_at, sections (system_pulse, recent_entries,
        open_questions, domain_activity, focus_domain, topic_search,
        pending_work, suggested_links, corcoran_graph)
    """
    _driver = driver or get_neo4j_driver()
    context = {"generated_at": datetime.now().isoformat(), "sections": {}}

    try:
        with _driver.session(database=ENTRY_DATABASE) as session:

            # === 1. System Pulse ===
            pulse = {}
            r = session.run("MATCH (s:StreamEntry) RETURN count(s) AS cnt")
            pulse["total_entries"] = r.single()["cnt"]

            r = session.run("MATCH (d:Domain) RETURN count(d) AS cnt")
            pulse["total_domains"] = r.single()["cnt"]

            r = session.run("MATCH (t:Tag) RETURN count(t) AS cnt")
            pulse["total_tags"] = r.single()["cnt"]

            r = session.run("MATCH (src:Source) RETURN count(src) AS cnt")
            pulse["total_sources"] = r.single()["cnt"]

            r = session.run(
                "MATCH (s:StreamEntry) "
                "RETURN max(s.id) AS latest_id"
            )
            pulse["latest_entry"] = r.single()["latest_id"]

            # Check APOC triggers
            try:
                r = session.run("CALL apoc.trigger.list() YIELD name RETURN collect(name) AS triggers")
                pulse["apoc_triggers"] = r.single()["triggers"]
            except Exception:
                pulse["apoc_triggers"] = "UNAVAILABLE"

            context["sections"]["system_pulse"] = pulse

            # === 2. Recent Entries (last 10 with connections) ===
            r = session.run(
                "MATCH (s:StreamEntry) "
                "WITH s ORDER BY s.id DESC LIMIT 10 "
                "OPTIONAL MATCH (s)-[:connectsTo|emergedFrom|resolves]->(linked:StreamEntry) "
                "OPTIONAL MATCH (s)-[:createdFile]->(f:File) "
                "OPTIONAL MATCH (s)-[:inDomain]->(d:Domain) "
                "RETURN s.id AS id, s.title AS title, s.type AS type, "
                "       s.status AS status, "
                "       collect(DISTINCT d.name) AS domains, "
                "       collect(DISTINCT linked.id) AS connected_to, "
                "       collect(DISTINCT f.path) AS files"
            )
            recent = []
            for rec in r:
                entry = dict(rec)
                if not entry["connected_to"] or entry["connected_to"] == [None]:
                    entry["connected_to"] = []
                if not entry["files"] or entry["files"] == [None]:
                    entry["files"] = []
                recent.append(entry)
            context["sections"]["recent_entries"] = recent

            # === 3. Open Questions ===
            r = session.run(
                "MATCH (q:StreamEntry) "
                "WHERE q.type = 'question' AND q.status IN ['active', 'open'] "
                "OPTIONAL MATCH (q)-[:resolves|connectsTo]-(related:StreamEntry) "
                "RETURN q.id AS id, q.title AS title, "
                "       collect(DISTINCT related.id) AS related_work "
                "ORDER BY q.id DESC"
            )
            questions = []
            for rec in r:
                q = dict(rec)
                if not q["related_work"] or q["related_work"] == [None]:
                    q["related_work"] = []
                questions.append(q)
            context["sections"]["open_questions"] = questions

            # === 4. Domain Activity ===
            r = session.run(
                "MATCH (s:StreamEntry)-[:inDomain]->(d:Domain) "
                "WITH d.name AS domain, s ORDER BY s.id DESC "
                "WITH domain, collect(s.id)[0..3] AS recent_ids, count(*) AS total "
                "RETURN domain, total, recent_ids "
                "ORDER BY total DESC"
            )
            domains = [dict(rec) for rec in r]
            context["sections"]["domain_activity"] = domains

            # === 5. Focus Domain (if specified) ===
            if focus_domain:
                r = session.run(
                    "MATCH (s:StreamEntry)-[:inDomain]->(d:Domain {name: $domain}) "
                    "WITH s ORDER BY s.id DESC LIMIT 15 "
                    "OPTIONAL MATCH (s)-[:connectsTo|emergedFrom]->(linked:StreamEntry) "
                    "RETURN s.id AS id, s.title AS title, s.type AS type, "
                    "       collect(DISTINCT linked.id) AS connections",
                    {"domain": focus_domain}
                )
                focus_entries = []
                for rec in r:
                    entry = dict(rec)
                    if not entry["connections"] or entry["connections"] == [None]:
                        entry["connections"] = []
                    focus_entries.append(entry)
                context["sections"]["focus_domain"] = {
                    "domain": focus_domain,
                    "entries": focus_entries
                }

            # === 6. Full-Text Search (if topic specified) ===
            if topic:
                r = session.run(
                    "CALL db.index.fulltext.queryNodes('streamEntry_fulltext', $topic) "
                    "YIELD node, score "
                    "RETURN node.id AS id, node.title AS title, node.type AS type, score "
                    "ORDER BY score DESC LIMIT 10",
                    {"topic": topic}
                )
                search_results = [dict(rec) for rec in r]
                context["sections"]["topic_search"] = {
                    "query": topic,
                    "results": search_results
                }

            # === 7. Pending Work ===
            r = session.run(
                "MATCH (s:StreamEntry) "
                "WHERE s.type IN ['idea', 'decision'] AND s.status = 'active' "
                "AND NOT (s)<-[:emergedFrom]-() "
                "WITH s ORDER BY s.id DESC LIMIT 10 "
                "RETURN s.id AS id, s.title AS title, s.type AS type"
            )
            pending = [dict(rec) for rec in r]
            context["sections"]["pending_work"] = pending

            # === 8. Active Project Phases ===
            try:
                r = session.run("""
                    MATCH (p:Phase)
                    WHERE p.status IN ['in_progress', 'blocked']
                    OPTIONAL MATCH (p)-[:DEPENDS_ON]->(dep:Phase)
                    WHERE dep.status <> 'complete'
                    OPTIONAL MATCH (p)<-[:DOCUMENTED_BY]-(entry:StreamEntry)
                    WITH p,
                         collect(DISTINCT dep.phaseNumber) AS blocking_deps,
                         collect(DISTINCT entry.id)[0..3] AS recent_entries
                    ORDER BY p.project, p.phaseNumber
                    RETURN p.phaseId AS phaseId,
                           p.project AS project,
                           p.phaseNumber AS phaseNumber,
                           p.title AS title,
                           p.status AS status,
                           p.blockedReason AS blockedReason,
                           toString(p.startedAt) AS startedAt,
                           p.sessionCount AS sessionCount,
                           blocking_deps,
                           recent_entries
                """)
                active_phases = []
                for rec in r:
                    phase = dict(rec)
                    phase["blocking_deps"] = [d for d in phase.get("blocking_deps", []) if d]
                    phase["recent_entries"] = [e for e in phase.get("recent_entries", []) if e]
                    active_phases.append(phase)

                if active_phases:
                    context["sections"]["active_phases"] = active_phases
            except Exception:
                pass  # Phase tracking is optional -- don't break session_start

            # === 9. Suggested Links ===
            r = session.run(
                "MATCH (a:StreamEntry)-[r:suggestsLink]->(b:StreamEntry) "
                "WHERE r.score >= 0.4 "
                "RETURN a.id AS from_id, a.title AS from_title, "
                "       b.id AS to_id, b.title AS to_title, "
                "       round(r.score * 1000) / 1000 AS score "
                "ORDER BY r.score DESC LIMIT 15"
            )
            suggestions = [dict(rec) for rec in r]
            context["sections"]["suggested_links"] = suggestions

        # === 10. Corcoran Knowledge Graph Summary ===
        try:
            with _driver.session(database=GRAPH_DATABASE) as csession:
                graph_summary = {}

                r = csession.run(
                    "MATCH (n) WHERE n:Person OR n:Organization OR n:Event "
                    "RETURN labels(n)[0] AS type, count(n) AS count "
                    "ORDER BY count DESC"
                )
                graph_summary["entity_counts"] = [dict(rec) for rec in r]

                r = csession.run(
                    "MATCH ()-[r]->() "
                    "WHERE type(r) IN ['EMPLOYED_BY','WORKED_AT','AFFILIATED_WITH','COLLABORATED_WITH','FAMILY_OF','RESOLVES_TO','MEMBER_OF','INVOLVED_IN','PART_OF'] "
                    "RETURN type(r) AS rel, count(r) AS count ORDER BY count DESC"
                )
                graph_summary["relationship_counts"] = [dict(rec) for rec in r]

                r = csession.run(
                    "MATCH (n)-[r]-() "
                    "WHERE (n:Person OR n:Organization) "
                    "WITH n, count(r) AS connections "
                    "ORDER BY connections DESC LIMIT 10 "
                    "RETURN n.name AS name, labels(n)[0] AS type, connections"
                )
                graph_summary["network_hubs"] = [dict(rec) for rec in r]

                r = csession.run(
                    "MATCH (n) WHERE n:Person OR n:Organization "
                    "AND n.addedDate IS NOT NULL "
                    "RETURN n.name AS name, labels(n)[0] AS type, n.source AS source, toString(n.addedDate) AS added "
                    "ORDER BY n.addedDate DESC LIMIT 5"
                )
                graph_summary["recent_additions"] = [dict(rec) for rec in r]

                r = csession.run(
                    "MATCH (n) WHERE n.researchStatus = 'needs-verification' "
                    "RETURN n.name AS name, labels(n)[0] AS type "
                    "LIMIT 10"
                )
                graph_summary["needs_verification"] = [dict(rec) for rec in r]

                context["sections"]["corcoran_graph"] = graph_summary
        except Exception as e:
            context["sections"]["corcoran_graph"] = {"error": str(e)}

    except Exception as e:
        context["error"] = str(e)
        import traceback
        context["traceback"] = traceback.format_exc()
    finally:
        if not driver:
            _driver.close()

    return context


# Subprocess entry point (backward compat with server.py dispatcher)
if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = session_start_impl(
        focus_domain=p.get("focus_domain"),
        topic=p.get("topic"),
    )
    output(r)
