#!/usr/bin/env python3
"""Recover context from a prior session's watcher data.

Reads the last (or specified) CoworkSession node's accumulated signals
and cross-references them against the current graph state to produce
actionable recovery context: uncommitted entities, unarchived sources,
error patterns to avoid, and what was already committed.

Claude makes all graph mutation decisions -- this tool only presents analysis.
---
description: Recover session context from watcher signals
databases: [lifestream, corcoran]
---
"""

import json
from lib.db import GRAPH_DATABASE, ENTRY_DATABASE
from lib.io import normalize_keys


def session_recover_impl(**kwargs):
    """Recover actionable context from a prior session.

    Parameters:
        session: str -- process name or sessionId to recover from (default: most recent)
        include_errors: bool -- include error signal analysis (default: true)
        include_sources: bool -- include source gap analysis (default: true)
        max_entities: int -- max entities to report (default: 50)
    """
    from lib.db import execute_read

    session_filter = kwargs.get("session")
    include_errors = kwargs.get("include_errors", True)
    include_sources = kwargs.get("include_sources", True)
    max_entities = kwargs.get("max_entities", 50)

    # -- Step 1: Find the target CoworkSession --
    if session_filter:
        # Match by processName or sessionId
        records, _ = execute_read(
            """
            MATCH (cs:CoworkSession)
            WHERE cs.processName = $filter OR cs.sessionId = $filter
            RETURN cs
            ORDER BY cs.createdAt DESC LIMIT 1
            """,
            database=ENTRY_DATABASE,
            filter=session_filter,
        )
    else:
        # Get the most recent session with watcher data (not the current one)
        # Current session has liveStatus = 'running'; we want the previous one
        records, _ = execute_read(
            """
            MATCH (cs:CoworkSession)
            WHERE cs.mentionedEntities IS NOT NULL
              AND (cs.liveStatus IS NULL OR cs.liveStatus <> 'running')
            RETURN cs
            ORDER BY COALESCE(cs.watcherLastUpdate, cs.createdAt) DESC LIMIT 1
            """,
            database=ENTRY_DATABASE,
        )

    if not records:
        return {
            "status": "no_data",
            "message": "No prior session with watcher data found. "
                       "The audit watcher must have run in a previous session to provide recovery context."
        }

    cs = dict(records[0]["cs"])
    process_name = cs.get("processName", "unknown")
    session_id = cs.get("sessionId", "")

    result = {
        "session": {
            "processName": process_name,
            "sessionId": session_id,
            "toolCallCount": cs.get("toolCallCount", 0),
            "userMessageCount": cs.get("userMessageCount", 0),
            "liveStatus": cs.get("liveStatus", "unknown"),
            "topTools": cs.get("topTools", [])[:5],
            "createdAt": str(cs.get("createdAt", "")),
        },
        "produced_entries": cs.get("producedEntries", []),
    }

    # -- Step 2: Cross-reference mentioned entities against corcoran graph --
    mentioned = cs.get("mentionedEntities", [])
    if mentioned:
        mentioned = mentioned[:max_entities]

        # Check which entities already exist in the graph
        entity_records, _ = execute_read(
            """
            UNWIND $names AS name
            OPTIONAL MATCH (n {name: name})
            WITH name, n,
                 CASE WHEN n IS NOT NULL
                      THEN labels(n)[0]
                      ELSE null
                 END AS label,
                 CASE WHEN n IS NOT NULL
                      THEN size([(n)-[:SUPPORTED_BY]->() | 1])
                      ELSE 0
                 END AS sourceCount
            RETURN name, n IS NOT NULL AS inGraph, label, sourceCount
            ORDER BY inGraph ASC, name
            """,
            database=GRAPH_DATABASE,
            names=mentioned,
        )

        in_graph = []
        not_in_graph = []
        weakly_sourced = []

        for r in entity_records:
            entry = {
                "name": r["name"],
                "label": r["label"],
                "sourceCount": r["sourceCount"],
            }
            if r["inGraph"]:
                in_graph.append(entry)
                if r["sourceCount"] == 0:
                    weakly_sourced.append(entry)
            else:
                not_in_graph.append(entry)

        result["entities"] = {
            "mentioned_total": len(mentioned),
            "in_graph": len(in_graph),
            "not_in_graph": not_in_graph,
            "weakly_sourced": weakly_sourced,
            "in_graph_details": in_graph,
        }
    else:
        result["entities"] = {
            "mentioned_total": 0,
            "in_graph": 0,
            "not_in_graph": [],
            "weakly_sourced": [],
            "in_graph_details": [],
        }

    # -- Step 3: Cross-reference captured sources against Source nodes --
    captured = cs.get("capturedSources", [])
    if include_sources and captured:
        # Check which URLs have Source nodes in corcoran
        source_records, _ = execute_read(
            """
            UNWIND $urls AS url
            OPTIONAL MATCH (s:Source)
            WHERE s.url = url OR s.url = replace(url, 'https://www.', 'https://')
                  OR s.url = replace(url, 'https://', 'https://www.')
            WITH url, s,
                 CASE WHEN s IS NOT NULL THEN s.archiveStatus ELSE null END AS status,
                 CASE WHEN s IS NOT NULL
                      THEN size([(s)<-[:SUPPORTED_BY]-() | 1])
                      ELSE 0
                 END AS usedBy
            RETURN url, s IS NOT NULL AS archived, status, usedBy
            ORDER BY archived ASC
            """,
            database=GRAPH_DATABASE,
            urls=captured[:100],  # Cap at 100 to avoid query size issues
        )

        archived_sources = []
        unarchived_sources = []
        unwired_sources = []  # Archived but not connected to any entity

        for r in source_records:
            if r["archived"]:
                entry = {"url": r["url"], "status": r["status"], "usedBy": r["usedBy"]}
                archived_sources.append(entry)
                if r["usedBy"] == 0:
                    unwired_sources.append(entry)
            else:
                unarchived_sources.append({"url": r["url"]})

        result["sources"] = {
            "captured_total": len(captured),
            "archived": len(archived_sources),
            "unarchived": unarchived_sources[:20],  # Cap output
            "unwired": unwired_sources[:20],  # Archived but no SUPPORTED_BY edges
        }
    else:
        result["sources"] = {"captured_total": len(captured), "note": "skipped" if not include_sources else "none captured"}

    # -- Step 4: Error signal analysis --
    errors_raw = cs.get("errorSignals", [])
    if include_errors and errors_raw:
        # Parse JSON strings back to dicts
        errors = []
        for e in errors_raw:
            try:
                errors.append(json.loads(e) if isinstance(e, str) else e)
            except (json.JSONDecodeError, TypeError):
                errors.append({"error": str(e)})

        # Group by error pattern (first 80 chars of error message)
        patterns = {}
        for e in errors:
            msg = e.get("error", "")[:80]
            if msg not in patterns:
                patterns[msg] = {"count": 0, "example": e.get("error", ""), "first_seen": e.get("timestamp")}
            patterns[msg]["count"] += 1

        # Sort by count descending
        sorted_patterns = sorted(patterns.values(), key=lambda x: x["count"], reverse=True)

        # Generate actionable guidance
        guidance = []
        for p in sorted_patterns:
            msg = p["example"]
            count = p["count"]
            if "Nested dict" in msg:
                guidance.append(f"[{count}x] Nested dict error: flatten 'properties' dict to top-level keys in graph tool params")
            elif "merge key" in msg.lower():
                guidance.append(f"[{count}x] Missing merge keys: check required fields before graph write (e.g., Property needs address+city+state)")
            elif "Unknown relationship type" in msg:
                rel_type = msg.split("'")[1] if "'" in msg else "?"
                guidance.append(f"[{count}x] Invalid rel type '{rel_type}': check lib/schema.py REL_TYPES for valid options")
            elif "not found" in msg.lower() and "Entity" in msg:
                guidance.append(f"[{count}x] Entity not found: create nodes before wiring relationships, or check spelling")
            elif "capture tiers failed" in msg.lower():
                guidance.append(f"[{count}x] All capture tiers failed: URL may be paywalled, bot-blocked, or dead. Check before re-attempting.")
            else:
                guidance.append(f"[{count}x] {msg[:120]}")

        result["errors"] = {
            "total": len(errors),
            "unique_patterns": len(sorted_patterns),
            "guidance": guidance[:10],
        }
    else:
        result["errors"] = {"total": len(errors_raw), "note": "skipped" if not include_errors else "none"}

    # -- Step 5: Summary --
    not_in_graph_count = len(result.get("entities", {}).get("not_in_graph", []))
    unarchived_count = len(result.get("sources", {}).get("unarchived", []))
    unwired_count = len(result.get("sources", {}).get("unwired", []))
    error_count = result.get("errors", {}).get("total", 0)

    summary_parts = []
    if not_in_graph_count:
        summary_parts.append(f"{not_in_graph_count} entities mentioned but not in graph")
    if unarchived_count:
        summary_parts.append(f"{unarchived_count} URLs fetched but not archived as Sources")
    if unwired_count:
        summary_parts.append(f"{unwired_count} Sources archived but not wired to any entity")
    if error_count:
        summary_parts.append(f"{error_count} tool errors (see guidance)")
    if not summary_parts:
        summary_parts.append("Session data recovered; no obvious gaps detected")

    result["summary"] = f"Session '{process_name}' ({cs.get('toolCallCount', 0)} tool calls): " + "; ".join(summary_parts)
    result["status"] = "ok"

    return normalize_keys(result)
