#!/usr/bin/env python3
"""Extract cost, token usage, and performance data from Cowork audit.jsonl files.

Parses `result` events from audit logs to provide per-session and aggregate
cost breakdowns, token usage patterns, and performance metrics.
---
description: Cost and token analysis across Cowork sessions
databases: [lifestream]
---
"""

import json
import os
import sys
import io
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.db import get_neo4j_driver, ENTRY_DATABASE
from lib.io import output, normalize_keys

from lib.session_detect import find_session_dir, scan_sessions


def _extract_costs(audit_path):
    """Extract result events with cost/token data from an audit.jsonl file."""
    results = []
    with open(audit_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") != "result":
                continue
            usage = entry.get("usage", {})
            results.append({
                "subtype": entry.get("subtype", ""),
                "is_error": entry.get("is_error", False),
                "cost_usd": entry.get("total_cost_usd", 0),
                "duration_ms": entry.get("duration_ms", 0),
                "duration_api_ms": entry.get("duration_api_ms", 0),
                "num_turns": entry.get("num_turns", 0),
                "input_tokens": usage.get("input_tokens", 0),
                "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "timestamp": entry.get("_audit_timestamp", ""),
            })
    return results


def _extract_compactions(audit_path):
    """Extract compaction events from audit.jsonl."""
    compactions = []
    with open(audit_path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") == "system" and entry.get("subtype") == "compact_boundary":
                meta = entry.get("compact_metadata", {})
                compactions.append({
                    "pre_tokens": meta.get("pre_tokens", 0),
                    "timestamp": entry.get("_audit_timestamp", ""),
                })
    return compactions


def session_costs_impl(session_id=None, top_n=10, driver=None, **kwargs):
    """Extract cost and token analysis from Cowork session audit files.

    Args:
        session_id: Optional specific session ID (or 'current' for active session).
                    If omitted, analyzes all sessions.
        top_n: Number of top sessions to return in rankings (default 10)
        driver: Optional shared Neo4j driver

    Returns:
        dict with per-session costs, aggregate totals, token breakdowns,
        and performance metrics
    """
    own_driver = False
    try:
        if driver is None:
            driver = get_neo4j_driver()
            own_driver = True

        session_dir = find_session_dir()
        if not session_dir:
            return {"error": "Could not find Cowork session directory"}

        # Build session map: sessionId -> {title, processName, auditPath}
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
            }

        # Filter to specific session if requested
        if session_id == "current":
            # Find current session via graph
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
                # Try partial match on processName
                matches = {k: v for k, v in sessions.items()
                           if session_id.lower() in v.get("processName", "").lower()
                           or session_id.lower() in v.get("title", "").lower()}
                if matches:
                    sessions = matches
                else:
                    return {"error": f"Session not found: {session_id}"}

        # Extract costs from all target sessions
        all_costs = {}
        all_compactions = {}
        for sid, info in sessions.items():
            costs = _extract_costs(info["auditPath"])
            compactions = _extract_compactions(info["auditPath"])
            if costs:
                all_costs[sid] = {
                    "title": info["title"],
                    "processName": info["processName"],
                    "results": costs,
                    "compactions": compactions,
                }

        # Aggregate per session
        session_summaries = []
        grand_total_cost = 0
        grand_total_input = 0
        grand_total_cache_create = 0
        grand_total_cache_read = 0
        grand_total_output = 0
        grand_total_turns = 0
        grand_total_duration_ms = 0

        for sid, data in all_costs.items():
            results = data["results"]
            total_cost = sum(r["cost_usd"] for r in results)
            total_turns = sum(r["num_turns"] for r in results)
            total_input = sum(r["input_tokens"] for r in results)
            total_cache_create = sum(r["cache_creation_tokens"] for r in results)
            total_cache_read = sum(r["cache_read_tokens"] for r in results)
            total_output = sum(r["output_tokens"] for r in results)
            total_duration = sum(r["duration_ms"] for r in results)
            error_count = sum(1 for r in results if r["is_error"])

            session_summaries.append({
                "sessionId": sid[:20] + "..." if len(sid) > 20 else sid,
                "title": data["title"],
                "processName": data["processName"],
                "cost_usd": round(total_cost, 4),
                "turns": total_turns,
                "interactions": len(results),
                "errors": error_count,
                "compactions": len(data["compactions"]),
                "input_tokens": total_input,
                "cache_creation_tokens": total_cache_create,
                "cache_read_tokens": total_cache_read,
                "output_tokens": total_output,
                "duration_minutes": round(total_duration / 60000, 1),
                "cost_per_turn": round(total_cost / max(total_turns, 1), 4),
            })

            grand_total_cost += total_cost
            grand_total_input += total_input
            grand_total_cache_create += total_cache_create
            grand_total_cache_read += total_cache_read
            grand_total_output += total_output
            grand_total_turns += total_turns
            grand_total_duration_ms += total_duration

        # Sort by cost descending
        session_summaries.sort(key=lambda x: x["cost_usd"], reverse=True)

        # Daily aggregation
        daily_costs = defaultdict(float)
        for sid, data in all_costs.items():
            for r in data["results"]:
                ts = r.get("timestamp", "")
                if ts:
                    day = ts[:10]  # YYYY-MM-DD
                    daily_costs[day] += r["cost_usd"]

        daily_sorted = sorted(daily_costs.items())

        result = {
            "sessions_analyzed": len(all_costs),
            "aggregate": {
                "total_cost_usd": round(grand_total_cost, 4),
                "total_turns": grand_total_turns,
                "total_input_tokens": grand_total_input,
                "total_cache_creation_tokens": grand_total_cache_create,
                "total_cache_read_tokens": grand_total_cache_read,
                "total_output_tokens": grand_total_output,
                "total_duration_minutes": round(grand_total_duration_ms / 60000, 1),
                "avg_cost_per_turn": round(grand_total_cost / max(grand_total_turns, 1), 4),
                "cache_hit_rate": round(
                    grand_total_cache_read / max(grand_total_cache_read + grand_total_cache_create + grand_total_input, 1) * 100, 1
                ),
            },
            "top_sessions_by_cost": session_summaries[:top_n],
            "daily_costs": [{"date": d, "cost_usd": round(c, 4)} for d, c in daily_sorted],
        }

        # If single session, include per-interaction detail
        if len(sessions) == 1:
            sid = list(all_costs.keys())[0]
            data = all_costs[sid]
            result["interactions"] = [
                {
                    "turns": r["num_turns"],
                    "cost_usd": round(r["cost_usd"], 4),
                    "output_tokens": r["output_tokens"],
                    "cache_read_tokens": r["cache_read_tokens"],
                    "duration_min": round(r["duration_ms"] / 60000, 1),
                    "timestamp": r["timestamp"][:19],
                }
                for r in data["results"]
            ]
            if data["compactions"]:
                result["compactions"] = data["compactions"]

        return normalize_keys(result)

    except Exception as e:
        return {"error": f"Session costs failed: {e}"}
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
    result = session_costs_impl(**params)
    output(result)


if __name__ == "__main__":
    main()
