"""Comprehensive audit of a session's work quality.
---
description: 13-check quality audit across both databases
databases: [lifestream, corcoran]
read_only: true
---
"""
import sys
from pathlib import Path
from datetime import datetime
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, GRAPH_DATABASE, ENTRY_DATABASE
from lib.io import setup_output, load_params, output
from lib.paths import LIFESTREAM_DIR
from lib.entries import entry_path
from lib.archives import ARCHIVE_MIN_TEXT_SIZE


def session_audit_impl(session_date=None, lifestream_dir=None, min_text_size=None,
                        driver=None, **kwargs):
    """Run a comprehensive audit of a session's work quality.

    Runs 13 checks across lifestream and corcoran databases + tool registry:
    1. Entries from session  2. Missing .md files  3. Findings without CITES
    4. Failed captures  5. Duplicate titles  6. No semantic edges  7. Null type
    8. Graph nodes without SUPPORTED_BY  9. Entries without DISCUSSES
    10. Weak provenance edges  11. Discussed stub entities
    12. Single-source weak entities  13. Tool registry drift

    Args:
        session_date: Session date as YYYY-MM-DD (default: today)
        lifestream_dir: Override for LIFESTREAM_DIR
        min_text_size: Override for ARCHIVE_MIN_TEXT_SIZE
        driver: Optional shared Neo4j driver

    Returns:
        dict with session, issues (list), summary
    """
    session_date = session_date or datetime.now().strftime("%Y-%m-%d")
    stream_dir = Path(lifestream_dir) if lifestream_dir else LIFESTREAM_DIR
    min_text = min_text_size or ARCHIVE_MIN_TEXT_SIZE

    _driver = driver or get_neo4j_driver()
    report = {"session": session_date, "issues": [], "summary": {}}

    try:
        with _driver.session(database=ENTRY_DATABASE) as session:

            # 1. Entries from this session
            result = session.run(
                "MATCH (s:StreamEntry) WHERE s.session = $date "
                "RETURN s.id, s.title, s.type, s.content",
                {"date": session_date}
            )
            entries = [dict(r) for r in result]
            report["summary"]["total_entries"] = len(entries)

            if not entries:
                report["summary"]["message"] = "No entries found for this session."
                return report

            entry_ids = [e["s.id"] for e in entries]

            # 2. Missing .md files
            missing_files = []
            for e in entries:
                eid = e["s.id"]
                md_path = entry_path(eid, base_dir=stream_dir)
                if not md_path.exists():
                    missing_files.append(eid)
            if missing_files:
                report["issues"].append({
                    "type": "missing_md_files",
                    "severity": "high",
                    "count": len(missing_files),
                    "entries": missing_files,
                    "fix": "Write .md files for these entries"
                })

            # 3. Findings/analyses without CITES edges
            result = session.run(
                "MATCH (s:StreamEntry) WHERE s.session = $date AND s.type IN ['finding', 'analysis'] "
                "OPTIONAL MATCH (s)-[:CITES]->(src:Source) "
                "WITH s, count(src) AS srcCount WHERE srcCount = 0 "
                "RETURN s.id, s.title",
                {"date": session_date}
            )
            unsourced = [dict(r) for r in result]
            if unsourced:
                report["issues"].append({
                    "type": "findings_without_sources",
                    "severity": "high",
                    "count": len(unsourced),
                    "entries": [u["s.id"] for u in unsourced],
                    "fix": "Add CITES edges with confidence tiers to these findings"
                })

            # 4. Source nodes with failed captures or low text
            # Exclude search_pdf captures -- PDFs with tiny textSize are expected
            # (scanned images where pymupdf extracts little), not pipeline failures
            result = session.run(
                "MATCH (s:Source) WHERE s.archiveStatus = 'failed' "
                "OR (s.textSize IS NOT NULL AND s.textSize < $min "
                "    AND COALESCE(s.captureMethod, '') <> 'search_pdf') "
                "RETURN s.url, s.archiveStatus, s.textSize, s.failureReason, "
                "       s.captureMethod",
                {"min": min_text}
            )
            failed_sources = [dict(r) for r in result]
            if failed_sources:
                report["issues"].append({
                    "type": "failed_captures",
                    "severity": "medium",
                    "count": len(failed_sources),
                    "sources": [{
                        "url": fs["s.url"],
                        "status": fs["s.archiveStatus"],
                        "text_size": fs["s.textSize"],
                        "reason": fs["s.failureReason"],
                        "capture_method": fs["s.captureMethod"]
                    } for fs in failed_sources],
                    "fix": "Re-archive these sources or mark them as training-knowledge confidence"
                })

            # 4b. Low-content PDF captures (informational, not alarming)
            result = session.run(
                "MATCH (s:Source) WHERE s.captureMethod = 'search_pdf' "
                "AND s.textSize IS NOT NULL AND s.textSize < $min "
                "RETURN s.url, s.textSize",
                {"min": min_text}
            )
            low_content = [dict(r) for r in result]
            if low_content:
                report["issues"].append({
                    "type": "low_content_captures",
                    "severity": "low",
                    "count": len(low_content),
                    "sources": [{"url": lc["s.url"], "text_size": lc["s.textSize"]}
                                for lc in low_content],
                    "fix": "These PDF captures have minimal extractable text (likely scanned images). "
                           "Content is archived but not searchable."
                })

            # 5. Duplicate titles within session
            title_counts = Counter(e["s.title"] for e in entries)
            duplicates = {t: c for t, c in title_counts.items() if c > 1}
            if duplicates:
                dup_entries = [e["s.id"] for e in entries if e["s.title"] in duplicates]
                report["issues"].append({
                    "type": "duplicate_titles",
                    "severity": "high",
                    "count": len(duplicates),
                    "titles": list(duplicates.keys()),
                    "entries": dup_entries,
                    "fix": "Delete duplicates, keep entry with better edge wiring"
                })

            # 6. Entries with no semantic edges
            result = session.run(
                "MATCH (s:StreamEntry) WHERE s.session = $date "
                "OPTIONAL MATCH (s)-[r:connectsTo|emergedFrom|resolves]-() "
                "WITH s, count(r) AS semEdges WHERE semEdges = 0 "
                "RETURN s.id, s.title, s.type",
                {"date": session_date}
            )
            orphans = [dict(r) for r in result]
            if orphans:
                report["issues"].append({
                    "type": "no_semantic_edges",
                    "severity": "medium",
                    "count": len(orphans),
                    "entries": [o["s.id"] for o in orphans],
                    "fix": "Wire connectsTo or emergedFrom edges to related entries"
                })

            # 7. Entries with null type
            null_type = [e["s.id"] for e in entries if not e.get("s.type")]
            if null_type:
                report["issues"].append({
                    "type": "null_entry_type",
                    "severity": "medium",
                    "count": len(null_type),
                    "entries": null_type,
                    "fix": "Set type property from .md frontmatter"
                })

        # -- Graph-quality checks (corcoran database) --
        with _driver.session(database=GRAPH_DATABASE) as cor:

            # 8. Graph nodes created today without SUPPORTED_BY edges
            result = cor.run(
                "MATCH (n) WHERE n.addedDate = $date "
                "AND (n:Person OR n:Organization OR n:Event) "
                "AND NOT (n)-[:SUPPORTED_BY]->(:Source) "
                "RETURN n.name AS name, labels(n) AS labels",
                {"date": session_date}
            )
            unsupported_nodes = [dict(r) for r in result]
            if unsupported_nodes:
                report["issues"].append({
                    "type": "graph_nodes_without_sources",
                    "severity": "high",
                    "count": len(unsupported_nodes),
                    "entities": [{"name": u["name"], "labels": u["labels"]} for u in unsupported_nodes],
                    "fix": "Wire SUPPORTED_BY edges to archived Source nodes for these entities"
                })

            # 9. Stream entries without DISCUSSES edges
            if entry_ids:
                result = cor.run(
                    "UNWIND $ids AS eid "
                    "OPTIONAL MATCH (ref:EntryRef {streamEntryId: eid})-[:DISCUSSES]->() "
                    "WITH eid, count(ref) AS refCount WHERE refCount = 0 "
                    "RETURN eid",
                    {"ids": entry_ids}
                )
                no_discusses = [r["eid"] for r in result]
                if no_discusses:
                    report["issues"].append({
                        "type": "entries_without_discusses",
                        "severity": "medium",
                        "count": len(no_discusses),
                        "entries": no_discusses,
                        "fix": "Use update_entry with discusses parameter to wire DISCUSSES edges"
                    })

            # 10. SUPPORTED_BY edges with weak provenance (upgrade candidates)
            result = cor.run(
                "MATCH (n)-[r:SUPPORTED_BY]->(s:Source) "
                "WHERE r.confidence IN ['web-search', 'training-knowledge'] "
                "RETURN n.name AS entity, r.confidence AS tier, r.claim AS claim, "
                "       s.url AS url, s.archiveStatus AS status "
                "ORDER BY CASE r.confidence "
                "  WHEN 'training-knowledge' THEN 0 "
                "  WHEN 'web-search' THEN 1 END "
                "LIMIT 20"
            )
            weak_provenance = [dict(r) for r in result]
            if weak_provenance:
                report["issues"].append({
                    "type": "weak_provenance_edges",
                    "severity": "low",
                    "count": len(weak_provenance),
                    "edges": [{
                        "entity": w["entity"],
                        "tier": w["tier"],
                        "claim": w["claim"],
                        "url": w["url"],
                        "archive_status": w["status"]
                    } for w in weak_provenance],
                    "fix": "Archive source URLs and upgrade provenance tier to archived-verified"
                })

            # 11. Discussed entities that are stubs (no description or wrong labels)
            if entry_ids:
                result = cor.run(
                    "UNWIND $ids AS eid "
                    "MATCH (ref:EntryRef {streamEntryId: eid})-[:DISCUSSES]->(n) "
                    "WHERE NOT (n:Person OR n:Organization OR n:Event) "
                    "   OR n.description IS NULL "
                    "RETURN ref.streamEntryId AS entry, n.name AS entity, labels(n) AS labels",
                    {"ids": entry_ids}
                )
                stub_entities = [dict(r) for r in result]
                if stub_entities:
                    report["issues"].append({
                        "type": "discussed_entities_without_detail",
                        "severity": "medium",
                        "count": len(stub_entities),
                        "entities": [{"entry": s["entry"], "name": s["entity"], "labels": s["labels"]}
                                     for s in stub_entities],
                        "fix": "Flesh out these entities with add_person/add_organization/add_event"
                    })

            # 12. Single-source entities with weak sourceType (Phase 5)
            result = cor.run("""
                MATCH (n)-[r:SUPPORTED_BY]->(s:Source)
                WHERE r.confidence IS NOT NULL
                WITH n.name AS entity, labels(n)[0] AS type,
                     collect(DISTINCT s.url) AS urls,
                     collect(DISTINCT s.sourceType) AS types
                WHERE size(urls) = 1
                  AND NONE(t IN types WHERE t IN ['primary-journalism', 'public-record', 'encyclopedic'])
                RETURN entity, type, urls[0] AS soleSource, types[0] AS sourceType
                ORDER BY entity
            """)
            single_weak = [dict(r) for r in result]
            if single_weak:
                report["issues"].append({
                    "type": "single_source_weak_entities",
                    "severity": "medium",
                    "count": len(single_weak),
                    "entities": [{"name": s["entity"], "type": s["type"],
                                  "source": s["soleSource"][:80], "sourceType": s["sourceType"]}
                                 for s in single_weak[:20]],
                    "fix": "These entities have only one source, and that source is not primary journalism, "
                           "public records, or encyclopedic. Consider finding additional sources."
                })

            # 13. Tool registry sync check
            try:
                from tools.core.registry_sync import registry_sync_impl
                sync_result = registry_sync_impl(action="validate")
                if sync_result.get("status") == "drift_detected":
                    drift_issues = []
                    if isinstance(sync_result.get("usage_md_issues"), list):
                        for ui in sync_result["usage_md_issues"]:
                            drift_issues.append(
                                f"{ui['group']}: missing USAGE.md sections for {ui['missing_sections']}"
                            )
                    if isinstance(sync_result.get("canonicalization_warnings"), list):
                        for cw in sync_result["canonicalization_warnings"]:
                            drift_issues.append(
                                f"{cw['operation']}: {cw['issue']}"
                            )
                    if drift_issues:
                        report["issues"].append({
                            "type": "tool_registry_drift",
                            "severity": "medium",
                            "count": len(drift_issues),
                            "details": drift_issues,
                            "fix": "Run registry_sync to identify and fix documentation drift"
                        })
                report["summary"]["registry_sync"] = {
                    "total_operations": sync_result.get("total_operations"),
                    "frontmatter_coverage": sync_result.get("frontmatter_coverage", {}).get("with", 0),
                }
            except Exception as sync_err:
                report["summary"]["registry_sync"] = f"check failed: {sync_err}"

        # Summary
        issue_count = sum(i["count"] for i in report["issues"])
        report["summary"]["issues_found"] = issue_count
        report["summary"]["issue_categories"] = len(report["issues"])
        report["summary"]["graph_checks_run"] = True
        if issue_count == 0:
            report["summary"]["verdict"] = "CLEAN -- no issues found"
        else:
            high = sum(1 for i in report["issues"] if i["severity"] == "high")
            report["summary"]["verdict"] = f"{issue_count} issues found ({high} high severity)"

    except Exception as e:
        report["error"] = str(e)
        import traceback
        report["traceback"] = traceback.format_exc()
    finally:
        if not driver:
            _driver.close()

    return report


# Subprocess entry point (backward compat with server.py dispatcher)
if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = session_audit_impl(
        session_date=p.get("session_date"),
        lifestream_dir=p.get("lifestream_dir"),
        min_text_size=p.get("min_text_size"),
    )
    output(r)

