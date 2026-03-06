"""
Layer 2 -- Reusable graph audit check functions.

Provides individual check functions that return lists of issue dicts.
Each check can be scoped (by session date, label filter, etc.) or run
against the full graph. Used by:
  - tools/graph/audit_ops.py (graph("audit") — full-graph or custom scope)
  - tools/workflow/session_audit.py (session-scoped audits)

Dependencies: lib.db (L0), lib.schema (L0)
"""
from lib.db import execute_read, GRAPH_DATABASE
from lib.schema import RESEARCH_LABELS, FIXTURE_LABELS, is_research_label


# ============================================================
# Scope Helpers
# ============================================================

def _research_label_filter():
    """Return a Cypher WHERE fragment filtering to research labels only."""
    labels_str = " OR ".join(f"n:{lbl}" for lbl in sorted(RESEARCH_LABELS))
    return f"({labels_str})"


def _build_scope_filter(scope, node_alias="n"):
    """Build optional WHERE clauses from a scope dict.

    Args:
        scope: dict with optional keys:
            - addedDate: "YYYY-MM-DD" — filter to nodes added on this date
            - labels: ["Person", "Organization"] — filter to specific labels
        node_alias: Cypher node variable name (default "n")

    Returns:
        (where_fragment, params_dict) — fragment starts with "AND ..." if non-empty
    """
    if not scope:
        return "", {}

    parts = []
    params = {}

    if "addedDate" in scope:
        parts.append(f"{node_alias}.addedDate = $scope_date")
        params["scope_date"] = scope["addedDate"]

    if "labels" in scope:
        label_filter = " OR ".join(f"{node_alias}:{lbl}" for lbl in scope["labels"])
        parts.append(f"({label_filter})")

    if parts:
        return " AND " + " AND ".join(parts), params
    return "", {}


# ============================================================
# Count Helper
# ============================================================

def _count_total(count_query, driver, database=GRAPH_DATABASE, **params):
    """Run a COUNT query and return the integer result."""
    records, _ = execute_read(count_query, database=database, driver=driver, **params)
    return records[0]["c"] if records else 0


# ============================================================
# Individual Check Functions
# ============================================================
# Each returns (items, total_count) where items is a list of issue dicts
# and total_count is the uncapped count of matching issues.

def check_unsupported_entities(driver, database=GRAPH_DATABASE, scope=None, limit=50):
    """Research entities without any SUPPORTED_BY edges to Source nodes.

    Returns:
        (items, total_count) — items is list of {name, labels} dicts.
    """
    scope_where, scope_params = _build_scope_filter(scope)
    research_filter = _research_label_filter()

    base_where = f"{research_filter} {scope_where} AND NOT (n)-[:SUPPORTED_BY]->(:Source)"

    query = f"""
        MATCH (n) WHERE {base_where}
        RETURN n.name AS name, labels(n) AS labels
        ORDER BY n.name
        LIMIT $limit
    """
    records, _ = execute_read(query, database=database, driver=driver,
                              limit=limit, **scope_params)
    items = [dict(r) for r in records]

    total = _count_total(f"MATCH (n) WHERE {base_where} RETURN count(n) AS c",
                         driver=driver, database=database, **scope_params)
    return items, total


def check_weak_provenance(driver, database=GRAPH_DATABASE, scope=None, limit=50):
    """SUPPORTED_BY edges at training-knowledge or web-search provenance tier.

    Returns:
        (items, total_count) — items is list of {entity, tier, claim, url, archive_status} dicts.
    """
    scope_where, scope_params = _build_scope_filter(scope)
    research_filter = _research_label_filter()

    base_where = (f"{research_filter} {scope_where} "
                  f"AND r.confidence IN ['web-search', 'training-knowledge']")

    query = f"""
        MATCH (n)-[r:SUPPORTED_BY]->(s:Source)
        WHERE {base_where}
        RETURN n.name AS entity, r.confidence AS tier, r.claim AS claim,
               s.url AS url, s.archiveStatus AS archive_status
        ORDER BY CASE r.confidence
          WHEN 'training-knowledge' THEN 0
          WHEN 'web-search' THEN 1 END
        LIMIT $limit
    """
    records, _ = execute_read(query, database=database, driver=driver,
                              limit=limit, **scope_params)
    items = [dict(r) for r in records]

    total = _count_total(
        f"MATCH (n)-[r:SUPPORTED_BY]->(s:Source) WHERE {base_where} RETURN count(r) AS c",
        driver=driver, database=database, **scope_params)
    return items, total


def check_single_source_entities(driver, database=GRAPH_DATABASE, scope=None, limit=50):
    """Research entities supported by only one source, and that source is not
    a strong type (primary-journalism, public-record, encyclopedic).

    Returns:
        (items, total_count) — items is list of {entity, label, sole_source, source_type} dicts.
    """
    scope_where, scope_params = _build_scope_filter(scope)
    research_filter = _research_label_filter()

    # This query has a WITH clause that makes it harder to split — use a shared CTE approach
    base_query = f"""
        MATCH (n)-[r:SUPPORTED_BY]->(s:Source)
        WHERE {research_filter} {scope_where}
        AND r.confidence IS NOT NULL
        WITH n.name AS entity, labels(n)[0] AS label,
             collect(DISTINCT s.url) AS urls,
             collect(DISTINCT s.sourceType) AS types
        WHERE size(urls) = 1
          AND NONE(t IN types WHERE t IN ['primary-journalism', 'public-record', 'encyclopedic'])
    """

    query = f"""
        {base_query}
        RETURN entity, label, urls[0] AS sole_source, types[0] AS source_type
        ORDER BY entity
        LIMIT $limit
    """
    records, _ = execute_read(query, database=database, driver=driver,
                              limit=limit, **scope_params)
    items = [dict(r) for r in records]

    count_query = f"{base_query} RETURN count(entity) AS c"
    total = _count_total(count_query, driver=driver, database=database, **scope_params)
    return items, total


def check_unclassified_sources(driver, database=GRAPH_DATABASE, limit=100):
    """Source nodes with null or 'unclassified' sourceType.

    Returns:
        (items, total_count) — items is list of {url, domain, current_type} dicts.
    """
    base_where = "s.sourceType IS NULL OR s.sourceType = 'unclassified'"

    query = f"""
        MATCH (s:Source) WHERE {base_where}
        RETURN s.url AS url, s.domain AS domain, s.sourceType AS current_type
        ORDER BY s.domain, s.url
        LIMIT $limit
    """
    records, _ = execute_read(query, database=database, driver=driver, limit=limit)
    items = [dict(r) for r in records]

    total = _count_total(
        f"MATCH (s:Source) WHERE {base_where} RETURN count(s) AS c",
        driver=driver, database=database)
    return items, total


def check_bare_nodes(driver, database=GRAPH_DATABASE, scope=None, limit=50):
    """Research nodes missing name or description properties.

    Returns:
        (items, total_count) — items is list of {name, labels, missing} dicts.
    """
    scope_where, scope_params = _build_scope_filter(scope)
    research_filter = _research_label_filter()

    base_where = f"{research_filter} {scope_where} AND (n.name IS NULL OR n.description IS NULL)"

    query = f"""
        MATCH (n) WHERE {base_where}
        RETURN n.name AS name, labels(n) AS labels,
               CASE
                 WHEN n.name IS NULL AND n.description IS NULL THEN 'name, description'
                 WHEN n.name IS NULL THEN 'name'
                 ELSE 'description'
               END AS missing
        ORDER BY n.name
        LIMIT $limit
    """
    records, _ = execute_read(query, database=database, driver=driver,
                              limit=limit, **scope_params)
    items = [dict(r) for r in records]

    total = _count_total(f"MATCH (n) WHERE {base_where} RETURN count(n) AS c",
                         driver=driver, database=database, **scope_params)
    return items, total


def check_orphan_nodes(driver, database=GRAPH_DATABASE, scope=None, limit=50):
    """Research nodes with zero relationships (completely disconnected).

    Returns:
        (items, total_count) — items is list of {name, labels} dicts.
    """
    scope_where, scope_params = _build_scope_filter(scope)
    research_filter = _research_label_filter()

    base_where = f"{research_filter} {scope_where} AND NOT (n)--()"

    query = f"""
        MATCH (n) WHERE {base_where}
        RETURN n.name AS name, labels(n) AS labels
        ORDER BY n.name
        LIMIT $limit
    """
    records, _ = execute_read(query, database=database, driver=driver,
                              limit=limit, **scope_params)
    items = [dict(r) for r in records]

    total = _count_total(f"MATCH (n) WHERE {base_where} RETURN count(n) AS c",
                         driver=driver, database=database, **scope_params)
    return items, total


def check_url_only_sources(driver, database=GRAPH_DATABASE, limit=50):
    """Source nodes with no successful archive (archiveStatus null or failed).

    Returns:
        (items, total_count) — items is list of {url, archive_status, failure_reason} dicts.
    """
    base_where = "s.archiveStatus IS NULL OR s.archiveStatus = 'failed'"

    query = f"""
        MATCH (s:Source)
        WHERE {base_where}
        RETURN s.url AS url, s.archiveStatus AS archive_status,
               s.failureReason AS failure_reason
        ORDER BY s.url
        LIMIT $limit
    """
    records, _ = execute_read(query, database=database, driver=driver, limit=limit)
    items = [dict(r) for r in records]

    total = _count_total(
        f"MATCH (s:Source) WHERE {base_where} RETURN count(s) AS c",
        driver=driver, database=database)
    return items, total


# ============================================================
# Registry of all checks
# ============================================================

CHECK_REGISTRY = {
    "unsupported": {
        "function": check_unsupported_entities,
        "severity": "high",
        "description": "Research entities without SUPPORTED_BY edges",
        "fix_hint": "Wire SUPPORTED_BY edges to archived Source nodes",
        "supports_scope": True,
    },
    "weak_provenance": {
        "function": check_weak_provenance,
        "severity": "low",
        "description": "SUPPORTED_BY edges at training-knowledge or web-search tier",
        "fix_hint": "Archive source URLs and upgrade provenance tier to archived-verified",
        "supports_scope": True,
    },
    "single_source": {
        "function": check_single_source_entities,
        "severity": "medium",
        "description": "Research entities with only one weak source",
        "fix_hint": "Find additional authoritative sources for these entities",
        "supports_scope": True,
    },
    "unclassified_sources": {
        "function": check_unclassified_sources,
        "severity": "medium",
        "description": "Source nodes with null or unclassified sourceType",
        "fix_hint": "Use graph('bulk_update') with SOURCE_DOMAIN_MAP to classify",
        "supports_scope": False,
    },
    "bare_nodes": {
        "function": check_bare_nodes,
        "severity": "medium",
        "description": "Research nodes missing name or description",
        "fix_hint": "Add missing properties via graph('node', {action: 'update', ...})",
        "supports_scope": True,
    },
    "orphans": {
        "function": check_orphan_nodes,
        "severity": "medium",
        "description": "Research nodes with zero relationships",
        "fix_hint": "Wire relationships or remove if created in error",
        "supports_scope": True,
    },
    "url_only": {
        "function": check_url_only_sources,
        "severity": "low",
        "description": "Source nodes without successful archives",
        "fix_hint": "Re-archive using research('archive', {url: ...})",
        "supports_scope": False,
    },
}
