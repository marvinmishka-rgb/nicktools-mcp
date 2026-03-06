#!/usr/bin/env python3
"""Ingest Cowork session metadata and audit stats into the lifestream graph.

Scans the Cowork session storage directory, reads session metadata JSONs and
audit.jsonl files, creates/updates CoworkSession nodes, wires PRECEDED_BY
temporal chain, links StreamEntries via PRODUCED_IN, and derives COVERED_TOPIC
edges to Domain nodes.

Designed to be idempotent -- safe to run repeatedly.
---
description: Ingest Cowork sessions into lifestream graph
databases: [lifestream]
---
"""

import json
import os
import sys
import io
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.db import get_neo4j_driver, ENTRY_DATABASE
from lib.io import output, normalize_keys

# -- Constants -----------------------------------------------------------
# Cowork stores sessions under AppData/Roaming/Claude/local-agent-mode-sessions/
# Structure: {account_uuid}/{org_uuid}/local_{session_uuid}.json  (metadata)
#            {account_uuid}/{org_uuid}/local_{session_uuid}/audit.jsonl  (conversation log)

from lib.session_detect import find_session_dir, scan_sessions as _scan_sessions_basic

DATABASE = ENTRY_DATABASE


def _scan_sessions(session_dir):
    """Scan all session metadata + audit files, return structured records."""
    sessions = []
    for item in sorted(os.listdir(session_dir)):
        if not item.startswith("local_") or not item.endswith(".json"):
            continue

        session_id = item[:-5]  # strip .json
        meta_path = os.path.join(session_dir, item)
        audit_path = os.path.join(session_dir, session_id, "audit.jsonl")

        # Read metadata
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue

        record = {
            "sessionId": session_id,
            "title": meta.get("title", ""),
            "processName": meta.get("processName", ""),
            "model": meta.get("model", ""),
        }

        # Convert epoch ms timestamps
        if meta.get("createdAt"):
            record["createdAtISO"] = datetime.fromtimestamp(
                meta["createdAt"] / 1000, tz=timezone.utc
            ).isoformat()
        if meta.get("lastActivityAt"):
            record["lastActivityAtISO"] = datetime.fromtimestamp(
                meta["lastActivityAt"] / 1000, tz=timezone.utc
            ).isoformat()

        # Parse audit.jsonl if it exists
        if os.path.exists(audit_path):
            record["auditPath"] = audit_path
            record["auditSizeKB"] = round(os.path.getsize(audit_path) / 1024)
            record.update(_parse_audit(audit_path))
        else:
            record["entryCount"] = 0
            record["userMessageCount"] = 0
            record["toolCallCount"] = 0
            record["topTools"] = []
            record["userTopics"] = []

        sessions.append(record)

    return sessions


def _parse_audit(audit_path):
    """Extract stats from an audit.jsonl file."""
    entry_count = 0
    user_msg_count = 0
    tool_calls = Counter()
    first_ts = None
    last_ts = None
    user_topics = []

    with open(audit_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            entry_count += 1

            ts = entry.get("_audit_timestamp")
            if ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

            t = entry.get("type")
            if t == "user":
                user_msg_count += 1
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    user_topics.append(content[:100])
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            user_topics.append(block["text"][:100])
                            break
            elif t == "assistant":
                for block in entry.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_calls[block.get("name", "unknown")] += 1

    return {
        "entryCount": entry_count,
        "userMessageCount": user_msg_count,
        "toolCallCount": sum(tool_calls.values()),
        "topTools": [t for t, _ in tool_calls.most_common(5)],
        "firstAuditTS": first_ts,
        "lastAuditTS": last_ts,
        "userTopics": user_topics[:5],
    }


def _ensure_indexes(driver):
    """Create indexes if they don't exist."""
    with driver.session(database=DATABASE) as session:
        session.run(
            "CREATE INDEX cowork_session_id IF NOT EXISTS "
            "FOR (cs:CoworkSession) ON (cs.sessionId)"
        )
        session.run(
            "CREATE INDEX cowork_session_process IF NOT EXISTS "
            "FOR (cs:CoworkSession) ON (cs.processName)"
        )


def _upsert_sessions(driver, sessions):
    """Create or update CoworkSession nodes."""
    created = 0
    updated = 0

    with driver.session(database=DATABASE) as session:
        for s in sessions:
            # MERGE on sessionId
            result = session.run("""
                MERGE (cs:CoworkSession {sessionId: $sessionId})
                ON CREATE SET
                    cs.title = $title,
                    cs.processName = $processName,
                    cs.model = $model,
                    cs.entryCount = $entryCount,
                    cs.userMessageCount = $userMessageCount,
                    cs.toolCallCount = $toolCallCount,
                    cs.topTools = $topTools,
                    cs.auditSizeKB = $auditSizeKB,
                    cs.userTopics = $userTopics,
                    cs._created = true
                ON MATCH SET
                    cs.entryCount = $entryCount,
                    cs.userMessageCount = $userMessageCount,
                    cs.toolCallCount = $toolCallCount,
                    cs.topTools = $topTools,
                    cs.auditSizeKB = $auditSizeKB,
                    cs._created = false
                RETURN cs._created AS wasCreated
            """, {
                "sessionId": s["sessionId"],
                "title": s.get("title", ""),
                "processName": s.get("processName", ""),
                "model": s.get("model", ""),
                "entryCount": s.get("entryCount", 0),
                "userMessageCount": s.get("userMessageCount", 0),
                "toolCallCount": s.get("toolCallCount", 0),
                "topTools": s.get("topTools", []),
                "auditSizeKB": s.get("auditSizeKB", 0),
                "userTopics": s.get("userTopics", []),
            })

            record = result.single()
            if record and record["wasCreated"]:
                created += 1
            else:
                updated += 1

            # Set datetime properties separately (need datetime() function)
            dt_sets = []
            dt_params = {"sessionId": s["sessionId"]}

            if s.get("createdAtISO"):
                dt_sets.append("cs.createdAt = datetime($createdAt)")
                dt_params["createdAt"] = s["createdAtISO"]
            if s.get("lastActivityAtISO"):
                dt_sets.append("cs.lastActivityAt = datetime($lastActivityAt)")
                dt_params["lastActivityAt"] = s["lastActivityAtISO"]
            if s.get("firstAuditTS"):
                dt_sets.append("cs.firstAuditTimestamp = datetime($firstAuditTS)")
                dt_params["firstAuditTS"] = s["firstAuditTS"]
            if s.get("lastAuditTS"):
                dt_sets.append("cs.lastAuditTimestamp = datetime($lastAuditTS)")
                dt_params["lastAuditTS"] = s["lastAuditTS"]
            if s.get("auditPath"):
                dt_sets.append("cs.auditPath = $auditPath")
                dt_params["auditPath"] = s["auditPath"]

            if dt_sets:
                session.run(
                    f"MATCH (cs:CoworkSession {{sessionId: $sessionId}}) SET {', '.join(dt_sets)}",
                    dt_params
                )

            # Clean up temp property
            session.run(
                "MATCH (cs:CoworkSession {sessionId: $sessionId}) REMOVE cs._created",
                {"sessionId": s["sessionId"]}
            )

    return created, updated


def _wire_preceded_by(driver):
    """Wire PRECEDED_BY chain between sessions ordered by createdAt."""
    with driver.session(database=DATABASE) as session:
        result = session.run("""
            MATCH (cs:CoworkSession)
            WHERE cs.createdAt IS NOT NULL
            WITH cs ORDER BY cs.createdAt
            WITH collect(cs) AS ordered
            UNWIND range(0, size(ordered)-2) AS i
            WITH ordered[i] AS earlier, ordered[i+1] AS later
            WHERE NOT (earlier)-[:PRECEDED_BY]->(later)
            CREATE (earlier)-[:PRECEDED_BY]->(later)
            RETURN count(*) AS wired
        """)
        record = result.single()
        return record["wired"] if record else 0


def _link_entries(driver):
    """Link StreamEntries to CoworkSessions via PRODUCED_IN by timestamp overlap."""
    with driver.session(database=DATABASE) as session:
        result = session.run("""
            MATCH (se:StreamEntry)
            WHERE se.createdAt IS NOT NULL OR se.timestamp IS NOT NULL
            WITH se, coalesce(se.createdAt, se.timestamp) AS entryTime
            WHERE entryTime IS NOT NULL
            MATCH (cs:CoworkSession)
            WHERE cs.createdAt IS NOT NULL AND cs.lastAuditTimestamp IS NOT NULL
            AND entryTime >= cs.createdAt
            AND entryTime <= cs.lastAuditTimestamp + duration('PT5M')
            WITH se, cs,
                 duration.between(cs.createdAt, entryTime).minutes AS minutesIn
            ORDER BY minutesIn
            WITH se, collect(cs)[0] AS bestSession
            WHERE bestSession IS NOT NULL
            AND NOT (se)-[:PRODUCED_IN]->(bestSession)
            CREATE (se)-[:PRODUCED_IN {linkedBy: 'session_ingest', linkedAt: datetime()}]->(bestSession)
            RETURN count(*) AS linked
        """)
        record = result.single()
        return record["linked"] if record else 0


def _wire_covered_topics(driver):
    """Derive COVERED_TOPIC edges from sessions to domains via their linked entries."""
    with driver.session(database=DATABASE) as session:
        result = session.run("""
            MATCH (se:StreamEntry)-[:PRODUCED_IN]->(cs:CoworkSession)
            MATCH (se)-[:inDomain]->(d:Domain)
            WITH cs, d, count(se) AS entryCount
            WHERE NOT (cs)-[:COVERED_TOPIC]->(d)
            CREATE (cs)-[:COVERED_TOPIC {entryCount: entryCount}]->(d)
            RETURN count(*) AS wired
        """)
        record = result.single()
        return record["wired"] if record else 0


def session_ingest_impl(mode="full", process_name=None, driver=None, **kwargs):
    """Ingest Cowork sessions into the lifestream graph.

    Args:
        mode: "full" (scan + create + link), "scan" (inventory only),
              "link" (only wire entries, skip node creation),
              "auto" (detect current/newest session, upsert just that one node)
        process_name: Optional process name to target in "auto" mode
        driver: Optional shared Neo4j driver

    Returns:
        dict with session counts, link counts, summary
    """
    own_driver = False
    try:
        if driver is None:
            driver = get_neo4j_driver()
            own_driver = True

        # Find session directory
        session_dir = find_session_dir()
        if not session_dir:
            return {"error": "Could not find Cowork session directory"}

        # Scan sessions
        sessions = _scan_sessions(session_dir)

        # Auto mode: detect current session, upsert just that one, return metadata
        if mode == "auto":
            from lib.session_detect import detect_current_session
            detected = detect_current_session(process_name=process_name)
            if not detected:
                return {"mode": "auto", "detected": False, "message": "No matching session found"}

            # Find the matching full scan record (has audit stats)
            target_id = detected["sessionId"]
            target_session = None
            for s in sessions:
                if s["sessionId"] == target_id:
                    target_session = s
                    break

            if not target_session:
                return {"mode": "auto", "detected": True, "ingested": False,
                        "message": f"Session {detected['processName']} found but not in scan results"}

            # Upsert just this session
            _ensure_indexes(driver)
            created, updated = _upsert_sessions(driver, [target_session])

            # Also wire PRECEDED_BY for this session
            _wire_preceded_by(driver)

            return normalize_keys({
                "mode": "auto",
                "detected": True,
                "ingested": True,
                "sessionId": target_session["sessionId"][:20] + "...",
                "processName": detected["processName"],
                "title": detected.get("title", ""),
                "model": detected.get("model", ""),
                "createdAt": detected.get("createdAt", ""),
                "auditSizeKB": detected.get("auditSizeKB", 0),
                "entryCount": target_session.get("entryCount", 0),
                "userMessageCount": target_session.get("userMessageCount", 0),
                "toolCallCount": target_session.get("toolCallCount", 0),
                "nodeCreated": created > 0,
                "nodeUpdated": updated > 0,
            })

        if mode == "scan":
            return normalize_keys({
                "sessionCount": len(sessions),
                "sessions": [
                    {
                        "sessionId": s["sessionId"][:20] + "...",
                        "title": s.get("title", ""),
                        "processName": s.get("processName", ""),
                        "entryCount": s.get("entryCount", 0),
                        "userMessageCount": s.get("userMessageCount", 0),
                        "toolCallCount": s.get("toolCallCount", 0),
                        "auditSizeKB": s.get("auditSizeKB", 0),
                    }
                    for s in sessions
                ]
            })

        # Ensure indexes
        _ensure_indexes(driver)

        results = {"sessionCount": len(sessions)}

        if mode in ("full",):
            # Phase 1: Upsert CoworkSession nodes
            created, updated = _upsert_sessions(driver, sessions)
            results["nodesCreated"] = created
            results["nodesUpdated"] = updated

            # Phase 2: Wire PRECEDED_BY chain
            preceded_by = _wire_preceded_by(driver)
            results["precededByWired"] = preceded_by

        if mode in ("full", "link"):
            # Phase 3: Link StreamEntries -> CoworkSessions
            linked = _link_entries(driver)
            results["entriesLinked"] = linked

            # Phase 4: Derive COVERED_TOPIC edges
            topics_wired = _wire_covered_topics(driver)
            results["coveredTopicsWired"] = topics_wired

        # Summary query
        with driver.session(database=DATABASE) as session:
            summary = session.run("""
                MATCH (cs:CoworkSession)
                OPTIONAL MATCH (cs)<-[:PRODUCED_IN]-(se:StreamEntry)
                WITH cs, count(se) AS linkedEntries
                RETURN count(cs) AS totalSessions,
                       sum(CASE WHEN linkedEntries > 0 THEN 1 ELSE 0 END) AS sessionsWithEntries,
                       sum(linkedEntries) AS totalLinkedEntries
            """)
            record = summary.single()
            if record:
                results["totalSessions"] = record["totalSessions"]
                results["sessionsWithEntries"] = record["sessionsWithEntries"]
                results["totalLinkedEntries"] = record["totalLinkedEntries"]

        return normalize_keys(results)

    except Exception as e:
        return {"error": f"Session ingest failed: {e}"}
    finally:
        if own_driver and driver:
            driver.close()


def main():
    """Subprocess entry point."""
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Missing params file path"}))
        sys.exit(1)

    try:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            params = json.load(f)
    except Exception as e:
        print(json.dumps({"error": f"Failed to load params: {e}"}))
        sys.exit(1)

    result = session_ingest_impl(**params)
    output(result)


if __name__ == "__main__":
    main()
