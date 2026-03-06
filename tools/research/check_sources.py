#!/usr/bin/env python3
"""
Standalone check_sources tool.
Checks if archived source URLs are still live.
Compares current page status against archived versions in Neo4j.
---
description: Check if archived source URLs are still live
databases: [corcoran]
read_only: true
---
"""

import json, sys, io, os, urllib.request, ssl
from pathlib import Path
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.db import get_neo4j_driver, ENTRY_DATABASE
from lib.io import load_params, output


def main():
    """
    CLI entry point. Reads params from stdin, checks sources, writes result to stdout.
    """
    try:
        params = load_params()
        domain = params.get("domain")
        entry_id = params.get("entry_id")

        driver = get_neo4j_driver()

        # Build query based on filters
        cypher = "MATCH (s:Source) "
        query_params = {}
        if entry_id:
            cypher = "MATCH (e:StreamEntry {id: $entry_id})-[:CITES]->(s:Source) "
            query_params["entry_id"] = entry_id
        elif domain:
            cypher += "WHERE s.domain = $domain "
            query_params["domain"] = domain

        cypher += "RETURN s.url AS url, s.title AS title, s.domain AS domain, s.archiveStatus AS status, s.lastCaptured AS lastCaptured ORDER BY s.domain, s.url"

        with driver.session(database=ENTRY_DATABASE) as session:
            result = session.run(cypher, query_params)
            sources = [dict(r) for r in result]

        driver.close()

        if not sources:
            output({"message": "No archived sources found matching filter", "count": 0})
            sys.exit(0)

        # Check each URL
        ctx = ssl.create_default_context()
        results = []
        for src in sources:
            url = src["url"]
            try:
                req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    status_code = resp.status
                    current_status = "live" if 200 <= status_code < 400 else "changed"
            except urllib.error.HTTPError as e:
                status_code = e.code
                current_status = "dead" if status_code in (404, 410, 403) else "error"
            except Exception as e:
                status_code = 0
                current_status = "unreachable"

            results.append({
                "url": url,
                "title": src["title"],
                "domain": src["domain"],
                "archived_status": src["status"],
                "current_status": current_status,
                "http_code": status_code,
                "changed": current_status != src.get("status", "live"),
            })

        # Summary
        live = sum(1 for r in results if r["current_status"] == "live")
        dead = sum(1 for r in results if r["current_status"] == "dead")
        changed = sum(1 for r in results if r["current_status"] in ("changed", "error", "unreachable"))

        output({
            "total": len(results),
            "live": live,
            "dead": dead,
            "changed_or_unreachable": changed,
            "sources": results,
        })

    except Exception as e:
        import traceback
        output({
            "error": str(e),
            "traceback": traceback.format_exc(),
        })
        sys.exit(1)


if __name__ == "__main__":
    main()
