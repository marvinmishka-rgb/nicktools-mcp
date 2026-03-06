"""Archive inventory: filesystem scan, Neo4j reconciliation, health dashboard.

Uses archive_handler.py (shared module) for discovery and reconciliation.
Exposed as archive_inventory MCP tool via server.py.

Capabilities:
  - List all archives by domain with file counts and sizes
  - Reconcile filesystem archives against Source nodes in Neo4j
  - Report orphan files, ghost Source nodes, status mismatches
  - Report failed/empty archives for re-attempt
  - Provide a dashboard summary
---
description: Inventory local archives, reconcile with Neo4j Source nodes
databases: [corcoran]
read_only: true
---
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, ENTRY_DATABASE
from lib.io import setup_output, load_params, output
from lib.paths import ARCHIVES_DIR
from lib.archives import discover_archives, reconcile_with_graph


def archive_inventory_impl(domain=None, reconcile=True, archives_dir=None,
                           driver=None, **kwargs):
    """Core logic: archive filesystem inventory with optional Neo4j reconciliation.

    Args:
        domain: Filter to specific domain (e.g. 'foxnews.com'). None = all.
        reconcile: Whether to reconcile against Neo4j Source nodes (default True).
        archives_dir: Override base archive directory.
        driver: Optional shared Neo4j driver. Created if None.

    Returns:
        dict with domain_summary, failed_archives, dashboard, and optionally
        reconciliation (orphan_files, ghost_sources, status_mismatches).
    """
    # 1. Discover all archives on disk
    archives = discover_archives(domain=domain, archives_dir=archives_dir)

    # 2. Build domain summary
    domains = {}
    failed = []
    total_html_bytes = 0
    total_text_bytes = 0

    for arch in archives:
        d = arch["domain"]
        if d not in domains:
            domains[d] = {"domain": d, "count": 0, "valid": 0, "failed": 0,
                          "html_bytes": 0, "text_bytes": 0}
        domains[d]["count"] += 1
        domains[d]["html_bytes"] += arch["html_size"]
        domains[d]["text_bytes"] += arch["text_size"]
        total_html_bytes += arch["html_size"]
        total_text_bytes += arch["text_size"]

        if arch["valid"]:
            domains[d]["valid"] += 1
        else:
            domains[d]["failed"] += 1
            failed.append({
                "url": arch["url"],
                "domain": d,
                "reason": arch["invalid_reason"],
                "text_size": arch["text_size"],
                "base_name": arch["base_name"],
            })

    # Sort domains by count descending
    domain_summary = sorted(domains.values(), key=lambda x: x["count"], reverse=True)

    # 3. Dashboard
    total_archives = len(archives)
    total_valid = sum(1 for a in archives if a["valid"])
    total_failed = total_archives - total_valid
    total_domains = len(domains)

    dashboard = {
        "total_archives": total_archives,
        "total_valid": total_valid,
        "total_failed": total_failed,
        "total_domains": total_domains,
        "total_html_bytes": total_html_bytes,
        "total_text_bytes": total_text_bytes,
        "total_size_mb": round((total_html_bytes + total_text_bytes) / (1024 * 1024), 2),
        "success_rate": round(total_valid / total_archives * 100, 1) if total_archives > 0 else 0,
    }

    result = {
        "dashboard": dashboard,
        "domain_summary": domain_summary,
        "failed_archives": failed,
    }

    # 4. Optional: reconcile with Neo4j
    if reconcile:
        _driver = driver or get_neo4j_driver()
        try:
            with _driver.session(database=ENTRY_DATABASE) as session:
                recon = reconcile_with_graph(session, archives=archives,
                                            archives_dir=archives_dir)
            result["reconciliation"] = recon
            # Add reconciliation stats to dashboard
            dashboard["orphan_files"] = recon["summary"]["orphan_count"]
            dashboard["ghost_sources"] = recon["summary"]["ghost_count"]
            dashboard["status_mismatches"] = recon["summary"]["mismatch_count"]
            dashboard["total_sources_in_db"] = recon["summary"]["total_sources_in_db"]
        except Exception as e:
            result["reconciliation"] = {"error": str(e)}
        finally:
            if not driver:
                _driver.close()

    return result


if __name__ == "__main__":
    setup_output()
    p = load_params()
    r = archive_inventory_impl(
        domain=p.get("domain"),
        reconcile=p.get("reconcile", True),
        archives_dir=p.get("archives_dir"),
    )
    output(r)
