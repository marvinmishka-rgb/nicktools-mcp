"""
Layer 2 -- Source-node edge wiring.

Depends on: lib.urls (Layer 1), lib.db (Layer 0).
Consolidates wire_supported_by (from config.py), wire_cites_edges + _validate_source
(from url_utils.py). This is the SINGLE home for all Source-node edge wiring --
eliminates the previous duplicate implementations.

v2: Migrated from session.run() to execute_read/execute_write (2026-03-02).
    Primary signature now takes driver= instead of session=. Legacy session=
    parameter still accepted for backward compatibility during transition.
"""
from lib.urls import (
    canonicalize_url, extract_domain, extract_path_keywords,
    fuzzy_match_source, VALID_PROVENANCE_TIERS
)
from lib.db import execute_read, execute_write, GRAPH_DATABASE, ENTRY_DATABASE


def _validate_source(src):
    """Validate and normalize a single source entry.

    Accepts either:
      - A plain URL string: "https://example.com/article"
      - A dict: {"url": "https://...", "confidence": "...", "claim": "..."}

    Returns (url, confidence, claim) tuple, or None if invalid.
    """
    # Accept plain URL strings -- normalize to dict
    if isinstance(src, str):
        src = {"url": src}
    if not isinstance(src, dict):
        return None
    src_url = src.get("url", "")
    if not src_url:
        return None
    confidence = src.get("confidence", "web-search")
    if confidence not in VALID_PROVENANCE_TIERS:
        confidence = "web-search"
    claim = src.get("claim", "")
    return src_url, confidence, claim


def wire_supported_by(entity_name, sources, match_clause=None, extra_params=None,
                      database=GRAPH_DATABASE, driver=None, session=None):
    """Wire SUPPORTED_BY edges from an entity to Source nodes.

    Reusable by add_person, add_organization, add_event, add_property, etc.
    Uses fuzzy URL matching to resolve against existing archived Source nodes.

    Args:
        entity_name: Name of the entity (used as $name in default match)
        sources: List of {url, confidence, claim} dicts
        match_clause: Optional custom MATCH clause (e.g. for Property nodes
            with composite keys). Must use 'n' as the node alias.
            Default: "MATCH (n {name: $name})"
        extra_params: Optional dict of additional Cypher params needed
            by a custom match_clause (e.g. {"city": "Scottsdale", "state": "AZ"})
        database: Neo4j database (default: corcoran)
        driver: Shared Neo4j driver (preferred path)
        session: DEPRECATED -- legacy Neo4j session. If provided without driver,
            falls back to session.run() for backward compat.

    Returns:
        (edges_wired: int, warnings: list[str])
    """
    wired = 0
    warnings = []

    # Backward compat: if caller passed session= but not driver=, use legacy path
    use_legacy = session is not None and driver is None

    for src in sources:
        validated = _validate_source(src)
        if not validated:
            continue
        src_url, confidence, claim = validated

        # Canonicalize URL before matching
        canonical = canonicalize_url(src_url)
        domain = extract_domain(canonical)
        path_keywords = extract_path_keywords(canonical)

        # Try exact URL match first -- try canonical form, then original
        exact_query = "MATCH (s:Source) WHERE s.url IN [$canonical, $original] RETURN s.url AS url LIMIT 1"
        exact_kw = {"canonical": canonical, "original": src_url}

        if use_legacy:
            result = session.run(exact_query, exact_kw)
            exact_match = result.single()
        else:
            records, _ = execute_read(exact_query, database=database, driver=driver, **exact_kw)
            exact_match = records[0] if records else None

        target_url = exact_match["url"] if exact_match else canonical

        # Auto-upgrade confidence if Source is already archived and no explicit
        # confidence was provided (i.e. it defaulted to 'web-search')
        if exact_match and confidence == "web-search":
            arch_query = "MATCH (s:Source {url: $url}) RETURN s.archiveStatus AS status LIMIT 1"
            if use_legacy:
                arch_result = session.run(arch_query, {"url": target_url})
                arch_record = arch_result.single()
            else:
                arch_records, _ = execute_read(arch_query, database=database, driver=driver, url=target_url)
                arch_record = arch_records[0] if arch_records else None

            if arch_record and arch_record["status"] in ("captured", "captured-via-wayback"):
                confidence = "archived-verified"

        if not exact_match and path_keywords:
            # Fuzzy match: look for archived Source on same domain
            fuzzy_query = (
                "MATCH (s:Source) WHERE s.domain = $domain "
                "AND s.archiveStatus IN ['captured', 'captured-via-wayback'] "
                "RETURN s.url AS url"
            )
            if use_legacy:
                fuzzy_result = session.run(fuzzy_query, {"domain": domain})
                candidates = list(fuzzy_result)
            else:
                candidates, _ = execute_read(fuzzy_query, database=database, driver=driver, domain=domain)

            for record in candidates:
                candidate_url = record["url"]
                matches = sum(1 for kw in path_keywords if kw.lower() in candidate_url.lower())
                if matches > 0:
                    target_url = candidate_url
                    if confidence == "web-search":
                        confidence = "archived-verified"
                    break

        if not exact_match:
            warnings.append(f"Source not found by exact URL: {src_url[:80]}...")

        # MERGE Source node and wire SUPPORTED_BY edge
        entity_match = match_clause or "MATCH (n {name: $name})"
        cypher = (
            "MERGE (source:Source {url: $url}) "
            "ON CREATE SET source.domain = $domain, source.archiveStatus = 'unarchived' "
            "WITH source "
            f"{entity_match} "
            "MERGE (n)-[r:SUPPORTED_BY]->(source) "
            "SET r.confidence = $confidence, r.claim = $claim, r.createdAt = datetime()"
        )
        params = {
            "url": target_url,
            "domain": domain,
            "name": entity_name,
            "confidence": confidence,
            "claim": claim,
        }
        if extra_params:
            params.update(extra_params)

        if use_legacy:
            session.run(cypher, params)
        else:
            execute_write(cypher, database=database, driver=driver, **params)
        wired += 1

    return wired, warnings


def wire_cites_edges(entry_id, sources, database=ENTRY_DATABASE, driver=None, session=None):
    """Wire CITES edges from a StreamEntry to Source nodes.

    Uses fuzzy_match_source() to resolve URLs against existing archived
    Source nodes before wiring.

    Args:
        entry_id: The StreamEntry ID (e.g. 'ls-20260225-001')
        sources: List of {url, confidence, claim} dicts
        database: Neo4j database (default: lifestream)
        driver: Shared Neo4j driver (preferred path)
        session: DEPRECATED -- legacy Neo4j session.

    Returns:
        (count_wired: int, warnings: list[str])
    """
    wired = 0
    warnings = []

    use_legacy = session is not None and driver is None

    for src in sources:
        validated = _validate_source(src)
        if not validated:
            continue
        src_url, confidence, claim = validated

        # Canonicalize before matching
        canonical = canonicalize_url(src_url)
        domain = extract_domain(canonical)

        if use_legacy:
            target_url = fuzzy_match_source(session, canonical)
        else:
            # fuzzy_match_source still expects a session -- bridge it
            from lib.db import get_neo4j_driver
            _drv = driver or get_neo4j_driver()
            with _drv.session(database=database) as _sess:
                target_url = fuzzy_match_source(_sess, canonical)

        if target_url != canonical:
            warnings.append(f"Fuzzy matched {canonical[:60]}... -> {target_url[:60]}...")

        cypher = (
            "MERGE (source:Source {url: $url}) "
            "ON CREATE SET source.domain = $domain, source.archiveStatus = 'unarchived' "
            "WITH source "
            "MATCH (entry:StreamEntry {id: $entry_id}) "
            "MERGE (entry)-[c:CITES]->(source) "
            "SET c.confidence = $confidence, c.claim = $claim, c.createdAt = datetime()"
        )
        params = {
            "url": target_url,
            "domain": domain,
            "entry_id": entry_id,
            "confidence": confidence,
            "claim": claim,
        }

        if use_legacy:
            session.run(cypher, params)
        else:
            execute_write(cypher, database=database, driver=driver, **params)
        wired += 1

    return wired, warnings
