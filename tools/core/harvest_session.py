#!/usr/bin/env python3
"""Harvest analytics from Cowork session audit logs.

Reads audit.jsonl files and extracts tool counts, keywords, domain signals,
entity mentions, and session statistics. Two modes:

- enrich: Update CoworkSession node in Neo4j with structured analytics properties.
- digest: Return a pre-formatted summary for create_entry consumption.

Reads from the archive directory first (ClaudeFiles/archives/cowork-sessions/),
falls back to AppData source if archive not found.

Designed to be idempotent -- re-harvesting updates properties, never duplicates.
---
description: Harvest analytics from session audit logs
databases: [lifestream, corcoran]
---
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.db import get_neo4j_driver, GRAPH_DATABASE, ENTRY_DATABASE
from lib.io import output, normalize_keys
from lib.paths import ARCHIVES_DIR
from lib.audit_parser import (
    generate_digest,
    extract_tool_counts,
    extract_user_keywords,
    extract_domain_signals,
    compute_session_stats,
)

# -- Constants -----------------------------------------------------------

from lib.session_detect import find_session_dir, scan_sessions

SESSION_ARCHIVE_DIR = ARCHIVES_DIR / "cowork-sessions"
DATABASE_LS = ENTRY_DATABASE
DATABASE_COR = GRAPH_DATABASE


def _resolve_audit_path(session_id=None, process_name=None):
    """
    Find the audit.jsonl path for a session.

    Checks archive first, then AppData source.
    Returns: (audit_path, session_id, metadata_dict) or raises ValueError.
    """
    session_dir = find_session_dir()

    # If process_name given, find session_id by scanning metadata
    if process_name and not session_id:
        if session_dir:
            for item in os.listdir(session_dir):
                if not item.startswith("local_") or not item.endswith(".json"):
                    continue
                meta_path = os.path.join(session_dir, item)
                try:
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                    if meta.get("processName") == process_name:
                        session_id = item[:-5]  # strip .json
                        break
                except Exception:
                    continue
        if not session_id:
            raise ValueError(f"No session found with processName '{process_name}'")

    # If no session_id, use the most recent session
    if not session_id:
        if not session_dir:
            raise ValueError("No sessions found")
        # Find newest by file modification time
        sessions = []
        for item in os.listdir(session_dir):
            if item.startswith("local_") and item.endswith(".json"):
                path = os.path.join(session_dir, item)
                sessions.append((os.path.getmtime(path), item[:-5]))
        if not sessions:
            raise ValueError("No sessions found")
        sessions.sort(reverse=True)
        session_id = sessions[0][1]

    # Read metadata
    metadata = {}
    if session_dir:
        meta_path = os.path.join(session_dir, f"{session_id}.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path, encoding="utf-8") as f:
                    metadata = json.load(f)
            except Exception:
                pass

    # Check archive first
    archive_path = SESSION_ARCHIVE_DIR / session_id / "audit.jsonl"
    if archive_path.exists():
        return str(archive_path), session_id, metadata

    # Fall back to AppData source
    if session_dir:
        source_path = os.path.join(session_dir, session_id, "audit.jsonl")
        if os.path.exists(source_path):
            return source_path, session_id, metadata

    raise ValueError(f"No audit.jsonl found for session '{session_id}'")


def _get_known_entities(driver):
    """Fetch entity names from corcoran for mention detection."""
    try:
        with driver.session(database=DATABASE_COR) as session:
            result = session.run("""
                MATCH (n)
                WHERE n.name IS NOT NULL
                  AND n:Person OR n:Organization OR n:Event
                RETURN n.name AS name
                LIMIT 500
            """)
            return [r["name"] for r in result]
    except Exception:
        return []


# -- Mode: enrich --------------------------------------------------------

def _enrich(audit_path, session_id, metadata, driver):
    """
    Extract analytics and update CoworkSession node with structured properties.
    """
    digest = generate_digest(audit_path, known_entities=_get_known_entities(driver))

    stats = digest["stats"]
    tool_counts = digest["tool_counts"]
    keywords = [kw for kw, _ in digest["keywords"]]
    domains = list(digest["domain_signals"].keys())
    entity_names = [e["name"] for e in digest["entity_mentions"][:20]]

    # Format tool breakdown as a compact string (Neo4j can't do nested maps)
    tool_summary = ", ".join(
        f"{name}: {count}"
        for name, count in sorted(tool_counts.items(), key=lambda x: -x[1])[:15]
    )

    # Format duration
    dur_min = stats.get("duration_minutes")
    if dur_min:
        hours = int(dur_min // 60)
        mins = int(dur_min % 60)
        duration_str = f"{hours}h {mins}m" if hours else f"{mins}m"
    else:
        duration_str = "unknown"

    # Update CoworkSession node
    with driver.session(database=DATABASE_LS) as session:
        session.run("""
            MATCH (cs:CoworkSession {sessionId: $sessionId})
            SET cs.toolBreakdown = $toolSummary,
                cs.keywords = $keywords,
                cs.domainSignals = $domains,
                cs.entityMentions = $entityNames,
                cs.durationMinutes = $durationMin,
                cs.durationFormatted = $durationStr,
                cs.avgResponseLength = $avgRespLen,
                cs.maxResponseLength = $maxRespLen,
                cs.harvestedAt = datetime()
        """, {
            "sessionId": session_id,
            "toolSummary": tool_summary,
            "keywords": keywords,
            "domains": domains,
            "entityNames": entity_names,
            "durationMin": dur_min or 0,
            "durationStr": duration_str,
            "avgRespLen": stats["avg_response_length"],
            "maxRespLen": stats["max_response_length"],
        })

    return {
        "status": "enriched",
        "session_id": session_id,
        "process_name": metadata.get("processName", ""),
        "stats": stats,
        "tool_breakdown": tool_summary,
        "keywords": keywords[:10],
        "domains": domains,
        "entities_found": len(entity_names),
        "duration": duration_str,
    }


# -- Mode: digest --------------------------------------------------------

def _digest(audit_path, session_id, metadata, driver):
    """
    Generate a pre-formatted summary for create_entry consumption.

    Returns structured data that Claude can use to write analytical entries
    without manually counting tools, listing topics, etc.
    """
    digest = generate_digest(audit_path, known_entities=_get_known_entities(driver))

    stats = digest["stats"]
    tool_counts = digest["tool_counts"]
    keywords = [kw for kw, _ in digest["keywords"]]
    domains = list(digest["domain_signals"].keys())

    # Format tool summary
    top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:10]
    tool_summary = f"{stats['tool_call_count']} tool calls: " + ", ".join(
        f"{count} {name}" for name, count in top_tools
    )

    # Format duration
    dur_min = stats.get("duration_minutes")
    if dur_min:
        hours = int(dur_min // 60)
        mins = int(dur_min % 60)
        duration_str = f"{hours}h {mins}m" if hours else f"{mins}m"
    else:
        duration_str = "unknown"

    # Suggest title from top keywords
    title_words = keywords[:5] if keywords else ["session"]
    suggested_title = " ".join(w.capitalize() for w in title_words[:3])

    # Suggest domains (top 3 by score)
    suggested_domains = domains[:3] if domains else ["general"]

    # Suggest tags from keywords not in domain keywords
    domain_kw_flat = set()
    from lib.audit_parser import DOMAIN_KEYWORDS
    for kws in DOMAIN_KEYWORDS.values():
        domain_kw_flat.update(kws)
    suggested_tags = [kw for kw in keywords[:10] if kw not in domain_kw_flat][:5]

    # Key topics from top keywords
    key_topics = keywords[:8]

    return {
        "status": "digest",
        "session_id": session_id,
        "process_name": metadata.get("processName", ""),
        "title": metadata.get("title", ""),
        "model": metadata.get("model", ""),
        "suggested_title": suggested_title,
        "suggested_domains": suggested_domains,
        "suggested_tags": suggested_tags,
        "tool_summary": tool_summary,
        "key_topics": key_topics,
        "duration": duration_str,
        "user_messages": stats["user_messages"],
        "assistant_messages": stats["assistant_messages"],
        "total_tool_calls": stats["tool_call_count"],
        "entity_mentions": [e["name"] for e in digest["entity_mentions"][:10]],
        "top_tools": dict(top_tools),
    }


# -- Main entry point ----------------------------------------------------

def harvest_session_impl(mode="enrich", session_id=None, process_name=None,
                         driver=None, **kwargs):
    """
    Harvest analytics from a Cowork session's audit log.

    Args:
        mode: "enrich" (update Neo4j) or "digest" (return summary for create_entry)
        session_id: e.g., "local_89387b67-9462-4ac7-b0ae-5fa69157394e"
        process_name: e.g., "compassionate-ecstatic-newton" (alternative to session_id)
        driver: Shared Neo4j driver

    If neither session_id nor process_name provided, uses the most recent session.
    """
    if not driver:
        driver = get_neo4j_driver()

    # Resolve audit file path
    audit_path, resolved_id, metadata = _resolve_audit_path(session_id, process_name)

    audit_size_mb = round(os.path.getsize(audit_path) / (1024 * 1024), 1)

    if mode == "enrich":
        result = _enrich(audit_path, resolved_id, metadata, driver)
    elif mode == "digest":
        result = _digest(audit_path, resolved_id, metadata, driver)
    else:
        return {"error": f"Unknown mode '{mode}'. Valid: enrich, digest"}

    result["audit_path"] = audit_path
    result["audit_size_mb"] = audit_size_mb
    return normalize_keys(result)


# -- Subprocess fallback -------------------------------------------------

def main():
    params = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
    result = harvest_session_impl(**params)
    output(result)


if __name__ == "__main__":
    main()
