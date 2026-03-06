#!/usr/bin/env python3
"""Full-text search across Cowork session audit.jsonl files.

Searches user messages, assistant text, thinking blocks, and tool_use_summaries
across all (or specific) sessions. Returns matched excerpts with session context
for Tier 3 audit mining.
---
description: Full-text search across Cowork session audit files
databases: [lifestream]
---
"""

import json
import os
import re
import sys
import io
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.db import get_neo4j_driver, ENTRY_DATABASE
from lib.io import output, normalize_keys

from lib.session_detect import find_session_dir, scan_sessions

# Maximum characters of context around a match
CONTEXT_WINDOW = 200
# Maximum matches per session before truncation
MAX_MATCHES_PER_SESSION = 20
# Maximum total matches across all sessions
MAX_TOTAL_MATCHES = 50


def _excerpt(text, match_start, match_end, context=CONTEXT_WINDOW):
    """Extract an excerpt around a match with surrounding context."""
    start = max(0, match_start - context)
    end = min(len(text), match_end + context)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _search_audit(audit_path, pattern, search_scope):
    """Search an audit.jsonl file for pattern matches.

    Args:
        audit_path: Path to the audit.jsonl file
        pattern: Compiled regex pattern
        search_scope: Set of content types to search
                     ('user', 'assistant', 'thinking', 'summaries', 'tools')

    Returns:
        List of match dicts with type, excerpt, timestamp
    """
    matches = []
    match_count = 0

    with open(audit_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if match_count >= MAX_MATCHES_PER_SESSION:
                break
            try:
                entry = json.loads(line)
            except Exception:
                continue

            ts = entry.get("_audit_timestamp", "")
            t = entry.get("type", "")

            # Search user messages
            if "user" in search_scope and t == "user":
                msg = entry.get("message", {})
                content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                if text:
                    for m in pattern.finditer(text):
                        matches.append({
                            "type": "user_message",
                            "excerpt": _excerpt(text, m.start(), m.end()),
                            "timestamp": ts[:19] if ts else "",
                        })
                        match_count += 1
                        if match_count >= MAX_MATCHES_PER_SESSION:
                            break

            # Search assistant text blocks
            if "assistant" in search_scope and t == "assistant":
                for block in entry.get("message", {}).get("content", []):
                    if match_count >= MAX_MATCHES_PER_SESSION:
                        break
                    if not isinstance(block, dict):
                        continue

                    # Text blocks
                    if block.get("type") == "text":
                        text = block.get("text", "")
                        for m in pattern.finditer(text):
                            matches.append({
                                "type": "assistant_text",
                                "excerpt": _excerpt(text, m.start(), m.end()),
                                "timestamp": ts[:19] if ts else "",
                            })
                            match_count += 1
                            if match_count >= MAX_MATCHES_PER_SESSION:
                                break

                    # Thinking blocks
                    if "thinking" in search_scope and block.get("type") == "thinking":
                        text = block.get("thinking", "")
                        for m in pattern.finditer(text):
                            matches.append({
                                "type": "thinking",
                                "excerpt": _excerpt(text, m.start(), m.end()),
                                "timestamp": ts[:19] if ts else "",
                            })
                            match_count += 1
                            if match_count >= MAX_MATCHES_PER_SESSION:
                                break

                    # Tool use inputs (search the tool name and input)
                    if "tools" in search_scope and block.get("type") == "tool_use":
                        tool_text = f"{block.get('name', '')} {json.dumps(block.get('input', {}))}"
                        for m in pattern.finditer(tool_text):
                            matches.append({
                                "type": "tool_use",
                                "tool": block.get("name", ""),
                                "excerpt": _excerpt(tool_text, m.start(), m.end()),
                                "timestamp": ts[:19] if ts else "",
                            })
                            match_count += 1
                            if match_count >= MAX_MATCHES_PER_SESSION:
                                break

            # Search tool_use_summary events
            if "summaries" in search_scope and t == "tool_use_summary":
                text = entry.get("summary", "")
                if text:
                    for m in pattern.finditer(text):
                        matches.append({
                            "type": "tool_summary",
                            "excerpt": _excerpt(text, m.start(), m.end()),
                            "timestamp": ts[:19] if ts else "",
                        })
                        match_count += 1
                        if match_count >= MAX_MATCHES_PER_SESSION:
                            break

    return matches


def session_search_impl(query, session_id=None, scope=None, case_sensitive=False,
                        max_results=None, driver=None, **kwargs):
    """Full-text search across Cowork session audit files.

    Args:
        query: Search string or regex pattern
        session_id: Optional session ID, process name fragment, or 'current'.
                    If omitted, searches all sessions.
        scope: List of content types to search. Options:
               'user', 'assistant', 'thinking', 'summaries', 'tools'
               Default: ['user', 'assistant', 'summaries']
        case_sensitive: Whether search is case-sensitive (default: False)
        max_results: Maximum total results (default: 50)
        driver: Optional shared Neo4j driver

    Returns:
        dict with matches grouped by session, total count, and search metadata
    """
    own_driver = False
    try:
        if driver is None:
            driver = get_neo4j_driver()
            own_driver = True

        if not query:
            return {"error": "Query string is required"}

        # Parse scope
        valid_scopes = {"user", "assistant", "thinking", "summaries", "tools"}
        search_scope = set(scope) if scope else {"user", "assistant", "summaries"}
        invalid = search_scope - valid_scopes
        if invalid:
            return {"error": f"Invalid scope(s): {invalid}. Valid: {valid_scopes}"}

        # Compile pattern
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pattern = re.compile(query, flags)
        except re.error as e:
            # Fall back to literal search if regex is invalid
            pattern = re.compile(re.escape(query), flags)

        max_hits = max_results or MAX_TOTAL_MATCHES

        # Find sessions
        session_dir = find_session_dir()
        if not session_dir:
            return {"error": "Could not find Cowork session directory"}

        sessions = {}
        for item in sorted(os.listdir(session_dir)):
            if not item.startswith("local_") or not item.endswith(".json"):
                continue
            sid = item[:-5]
            meta_path = os.path.join(session_dir, item)
            audit_path = os.path.join(session_dir, sid, "audit.jsonl")

            try:
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue

            if not os.path.exists(audit_path):
                continue

            sessions[sid] = {
                "title": meta.get("title", ""),
                "processName": meta.get("processName", ""),
                "auditPath": audit_path,
                "auditSize": os.path.getsize(audit_path),
            }

        # Filter to specific session if requested
        if session_id == "current":
            with driver.session(database=ENTRY_DATABASE) as db_session:
                rec = db_session.run("""
                    MATCH (cs:CoworkSession)
                    WHERE cs.lastAuditTimestamp IS NOT NULL
                    RETURN cs.sessionId AS sid
                    ORDER BY cs.lastAuditTimestamp DESC LIMIT 1
                """).single()
                if rec:
                    session_id = rec["sid"]

        if session_id and session_id != "current":
            if session_id in sessions:
                sessions = {session_id: sessions[session_id]}
            else:
                matches = {k: v for k, v in sessions.items()
                           if session_id.lower() in v.get("processName", "").lower()
                           or session_id.lower() in v.get("title", "").lower()}
                if matches:
                    sessions = matches
                else:
                    return {"error": f"Session not found: {session_id}"}

        # Search sessions (largest/most recent first for relevance)
        sorted_sessions = sorted(
            sessions.items(),
            key=lambda x: x[1].get("auditSize", 0),
            reverse=True
        )

        all_matches = {}
        total_hits = 0
        sessions_searched = 0
        sessions_with_hits = 0

        for sid, info in sorted_sessions:
            if total_hits >= max_hits:
                break

            sessions_searched += 1
            matches = _search_audit(info["auditPath"], pattern, search_scope)

            if matches:
                sessions_with_hits += 1
                # Trim to stay within total limit
                remaining = max_hits - total_hits
                if len(matches) > remaining:
                    matches = matches[:remaining]

                all_matches[sid] = {
                    "title": info["title"],
                    "processName": info["processName"],
                    "match_count": len(matches),
                    "matches": matches,
                }
                total_hits += len(matches)

        result = {
            "query": query,
            "scope": sorted(search_scope),
            "case_sensitive": case_sensitive,
            "sessions_searched": sessions_searched,
            "sessions_with_hits": sessions_with_hits,
            "total_matches": total_hits,
            "truncated": total_hits >= max_hits,
            "results": all_matches,
        }

        return normalize_keys(result)

    except Exception as e:
        return {"error": f"Session search failed: {e}"}
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
    result = session_search_impl(**params)
    output(result)


if __name__ == "__main__":
    main()
