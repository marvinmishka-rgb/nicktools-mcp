"""
Layer 1 -- Entity name matcher for auto-wiring DISCUSSES edges.

Depends on: lib.db (Layer 0).

Scans text against known corcoran graph entity names and returns matches.
Entity names are cached in memory with a 10-minute TTL for performance.
"""
import time
import re
from lib.db import get_neo4j_driver, GRAPH_DATABASE


# -- Cache --

_entity_cache = None
_cache_timestamp = 0
_CACHE_TTL = 600  # 10 minutes

# Names too short or too common to match reliably as substrings
_MIN_NAME_LENGTH = 5
_SKIP_NAMES = frozenset([
    "nCube",  # too short / ambiguous
])


def invalidate_cache():
    """Force refresh of entity name cache on next call."""
    global _entity_cache, _cache_timestamp
    _entity_cache = None
    _cache_timestamp = 0


def _load_entities(driver=None):
    """Load all named entities from corcoran graph.

    Returns list of {name, labels} sorted by name length descending
    (longer names matched first to avoid partial matches).
    """
    global _entity_cache, _cache_timestamp

    now = time.time()
    if _entity_cache is not None and (now - _cache_timestamp) < _CACHE_TTL:
        return _entity_cache

    _driver = driver or get_neo4j_driver()
    own_driver = driver is None

    try:
        with _driver.session(database=GRAPH_DATABASE) as session:
            result = session.run(
                "MATCH (n) WHERE n.name IS NOT NULL "
                "RETURN n.name AS name, labels(n) AS labels"
            )
            entities = []
            for record in result:
                name = record["name"]
                labels = record["labels"]
                if len(name) < _MIN_NAME_LENGTH:
                    continue
                if name in _SKIP_NAMES:
                    continue
                entities.append({"name": name, "labels": labels})

            # Sort by name length descending -- match longer names first
            entities.sort(key=lambda e: len(e["name"]), reverse=True)

            _entity_cache = entities
            _cache_timestamp = now
            return entities
    finally:
        if own_driver:
            _driver.close()


def find_entities(text, driver=None, min_confidence=1):
    """Find mentions of known graph entities in text.

    Uses case-insensitive substring matching with word boundary awareness.
    Longer entity names are checked first to avoid partial match conflicts.

    Args:
        text: The text to scan (typically entry title + content)
        driver: Optional shared Neo4j driver
        min_confidence: Minimum match quality (reserved for future use)

    Returns:
        list of {name, labels, match_type} dicts
    """
    if not text or not text.strip():
        return []

    entities = _load_entities(driver=driver)
    if not entities:
        return []

    text_lower = text.lower()
    matches = []
    # Track matched spans to avoid overlapping matches
    matched_spans = []

    for entity in entities:
        name = entity["name"]
        name_lower = name.lower()

        # Find all occurrences
        start = 0
        found = False
        while True:
            idx = text_lower.find(name_lower, start)
            if idx == -1:
                break

            end = idx + len(name_lower)

            # Word boundary check: character before/after should be
            # non-alphanumeric (or start/end of string)
            before_ok = (idx == 0 or not text[idx - 1].isalnum())
            after_ok = (end >= len(text) or not text[end].isalnum())

            if before_ok and after_ok:
                # Check this span doesn't overlap with an already-matched longer name
                overlap = False
                for (ms, me) in matched_spans:
                    if idx >= ms and idx < me:
                        overlap = True
                        break
                    if end > ms and end <= me:
                        overlap = True
                        break

                if not overlap:
                    matched_spans.append((idx, end))
                    found = True

            start = idx + 1

        if found:
            matches.append({
                "name": entity["name"],
                "labels": entity["labels"],
                "match_type": "substring"
            })

    return matches


def find_entities_batch(texts, driver=None):
    """Find entities across multiple texts, deduplicating results.

    Args:
        texts: List of text strings to scan
        driver: Optional shared Neo4j driver

    Returns:
        list of unique {name, labels, match_type} dicts
    """
    seen = set()
    results = []
    for text in texts:
        for match in find_entities(text, driver=driver):
            if match["name"] not in seen:
                seen.add(match["name"])
                results.append(match)
    return results
