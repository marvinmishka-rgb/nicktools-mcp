"""Run graph quality checks across the full graph or a filtered scope.
---
description: Full-graph or scoped quality audit filtering to research entities only
databases: [GRAPH_DATABASE]
read_only: true
---

Replaces the manual Cypher patterns from the graph-commit skill and extends
session_audit's graph checks to work without a session date filter.

All checks automatically filter to RESEARCH_LABELS — fixture nodes (Agent,
Neighborhood, Market, Region) are excluded from quality scrutiny.

Usage:
    graph("audit")                              # all checks, full graph
    graph("audit", {"checks": ["unsupported"]}) # specific check
    graph("audit", {"scope": {"addedDate": "2026-03-04"}})  # session scope
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import GRAPH_DATABASE


def _generate_suggested_calls(check_name, items):
    """Generate pre-formatted tool call suggestions for each audit item.

    Returns a list of suggested call strings (max 10 to keep output manageable).
    """
    calls = []
    for item in items[:10]:
        name = item.get("name") or item.get("entity") or item.get("url", "")
        if not name:
            continue

        if check_name == "unsupported":
            calls.append(
                f'graph("wire_evidence", {{"entity": "{name}", "sources": ["URL"]}})'
            )
        elif check_name == "bare_nodes":
            missing = item.get("missing", "description")
            if "name" in missing:
                calls.append(
                    f'graph("node", {{"action": "update", "name": "{name}", '
                    f'"description": "..."}})'
                )
            else:
                calls.append(
                    f'graph("node", {{"action": "update", "name": "{name}", '
                    f'"description": "..."}})'
                )
        elif check_name == "orphans":
            label = item.get("labels", [""])[0] if item.get("labels") else ""
            calls.append(
                f'graph("rel", {{"from": "{name}", "rel": "REL_TYPE", "to": "TARGET"}})'
            )
        elif check_name == "weak_provenance":
            url = item.get("url", "")
            calls.append(
                f'research("archive", {{"url": "{url}"}})'
            )
        elif check_name == "unclassified_sources":
            url = item.get("url", "")
            calls.append(
                f'query("MATCH (s:Source {{url: \'{url}\'}}) SET s.sourceType = \'TYPE\'", mode="write")'
            )
        elif check_name == "url_only":
            url = item.get("url", "")
            calls.append(
                f'research("archive", {{"url": "{url}"}})'
            )
        elif check_name == "single_source":
            calls.append(
                f'research("read", {{"url": "ADDITIONAL_SOURCE_URL", "archive": true}})'
            )

    return calls


def audit_impl(checks=None, scope=None, limit=50, database=GRAPH_DATABASE,
               driver=None, **kwargs):
    """Run graph quality checks across the full graph or a filtered scope.

    Args:
        checks: List of check names to run, or None for all.
                Valid: unsupported, weak_provenance, single_source,
                       unclassified_sources, bare_nodes, orphans, url_only
        scope: Optional filter dict:
               {"addedDate": "2026-03-04"} — session scope
               {"labels": ["Person", "Organization"]} — label filter
               None — full graph (research labels only by default)
        limit: Max results per check (default 50)
        database: Neo4j database (default: GRAPH_DATABASE)
        driver: Optional shared Neo4j driver

    Returns:
        dict with:
        - checks: [{name, severity, description, count, items, fix_hint}]
        - summary: {total_issues, checks_run, high/medium/low counts}
    """
    from lib.audit_engine import CHECK_REGISTRY

    # Determine which checks to run
    if checks:
        if isinstance(checks, str):
            checks = [checks]
        invalid = [c for c in checks if c not in CHECK_REGISTRY]
        if invalid:
            return {
                "error": f"Unknown check(s): {invalid}. "
                         f"Valid: {sorted(CHECK_REGISTRY.keys())}"
            }
        check_names = checks
    else:
        check_names = list(CHECK_REGISTRY.keys())

    results = []
    severity_counts = {"high": 0, "medium": 0, "low": 0}
    total_issues = 0

    for name in check_names:
        check_info = CHECK_REGISTRY[name]
        fn = check_info["function"]
        severity = check_info["severity"]

        # Build kwargs for the check function
        fn_kwargs = {"driver": driver, "database": database, "limit": limit}
        if check_info.get("supports_scope"):
            fn_kwargs["scope"] = scope

        try:
            items, total_count = fn(**fn_kwargs)
            count = len(items)
        except Exception as e:
            results.append({
                "name": name,
                "severity": severity,
                "description": check_info["description"],
                "count": 0,
                "total_count": 0,
                "error": str(e),
            })
            continue

        total_issues += total_count
        if total_count > 0:
            severity_counts[severity] += total_count

        # Generate suggested tool calls for each item
        suggested_calls = _generate_suggested_calls(name, items)

        entry = {
            "name": name,
            "severity": severity,
            "description": check_info["description"],
            "count": count,
            "total_count": total_count,
            "items": items,
            "fix_hint": check_info["fix_hint"],
        }
        if suggested_calls:
            entry["suggested_calls"] = suggested_calls

        results.append(entry)

    return {
        "checks": results,
        "summary": {
            "total_issues": total_issues,
            "checks_run": len(check_names),
            "high": severity_counts["high"],
            "medium": severity_counts["medium"],
            "low": severity_counts["low"],
            "scope": scope or "full graph (research labels only)",
        },
    }


if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = audit_impl(**params)
    output(result)
