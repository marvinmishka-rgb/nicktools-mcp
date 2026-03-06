"""
Layer 1 -- URL parsing, domain extraction, source classification.

Depends on: Layer 0 only (no lib/ imports needed for URL logic).
Consolidates URL functions from config.py + fuzzy_match_source from url_utils.py.
"""
import re


# -- Constants --

VALID_PROVENANCE_TIERS = {"archived-verified", "web-search", "training-knowledge"}

# Source reliability classification
# Used to auto-set sourceType on Source nodes during archiving.
# Domain -> sourceType mapping. Queried by extract_domain() output (no www.).
#
# CUSTOMIZATION: Edit this map for your research domain. The categories below
# (primary-journalism, encyclopedic, public-record, etc.) reflect source reliability
# tiers. Add your domain-specific sources and classify them accordingly.
# See docs/CUSTOMIZATION.md for guidance.
SOURCE_TYPE_MAP = {
    # Primary journalism (Pulitzer-caliber, editorial standards, fact-checking)
    "newyorker.com": "primary-journalism",
    "nytimes.com": "primary-journalism",
    "washingtonpost.com": "primary-journalism",
    "therealdeal.com": "primary-journalism",
    "chicagobusiness.com": "primary-journalism",
    "chicagotribune.com": "primary-journalism",
    "azcentral.com": "primary-journalism",
    "foxnews.com": "primary-journalism",
    "nbcnews.com": "primary-journalism",
    "npr.org": "primary-journalism",

    # Encyclopedic (editorial review, sourced claims)
    "wikipedia.org": "encyclopedic",
    "britannica.com": "encyclopedic",

    # Public records (government sources, official filings)
    "patents.justia.com": "public-record",
    "opencorporates.com": "public-record",
    "azcc.gov": "public-record",
    "dos.ny.gov": "public-record",
    "sec.gov": "public-record",
    "courtlistener.com": "public-record",

    # Investigative / advocacy research (mission-driven but researched)
    "politicalresearch.org": "advocacy-research",
    "influencewatch.org": "advocacy-research",
    "sourcewatch.org": "advocacy-research",

    # Tabloid / celebrity press (entertainment framing, light sourcing)
    "nickiswift.com": "tabloid",
    "thelist.com": "tabloid",
    "dailymail.co.uk": "tabloid",

    # Blog with factual substrate (opinion + some verifiable claims)
    "helenaglass.net": "blog-factual-substrate",
    "wltreport.com": "blog-factual-substrate",

    # AI-generated (LLM output, high hallucination risk)
    "factually.co": "ai-generated",
    "grokipedia.com": "ai-generated",

    # Leaked documents (unauthorized disclosures)
    "jar2.com": "leaked-document",

    # News wire / aggregation
    "8pmnews.com": "news-aggregation",
    "apnews.com": "news-aggregation",
    "reuters.com": "news-aggregation",

    # Partisan media (explicit political alignment)
    "breitbart.com": "partisan-media",
    "washingtontimes.com": "partisan-media",

    # Professional / industry (trade publications, professional networks)
    "linkedin.com": "professional-network",
    "corcoran.com": "industry-source",
    "compass.com": "industry-source",

    # Agent directories (real estate listing/search platforms)
    "streeteasy.com": "agent-directory",
    "zillow.com": "agent-directory",
    "loopnet.com": "agent-directory",
    "homes.com": "agent-directory",
    "showcase.com": "agent-directory",
    "homesandland.com": "agent-directory",
    "triplemint.com": "agent-directory",
    "realtor.com": "agent-directory",
    "redfin.com": "agent-directory",
    "trulia.com": "agent-directory",

    # Default
    "_default": "unclassified",
}

VALID_SOURCE_TYPES = {
    "primary-journalism", "encyclopedic", "public-record",
    "advocacy-research", "tabloid", "blog-factual-substrate",
    "ai-generated", "leaked-document", "news-aggregation",
    "partisan-media", "professional-network", "industry-source",
    "agent-directory", "unclassified",
}


# -- Functions --

def canonicalize_url(url):
    """Normalize a URL to a canonical form for Source node matching.

    Strips:
    - Wayback Machine prefix (web.archive.org/web/TIMESTAMP/)
    - Trailing slashes (preserving bare domain)
    - Common tracking params (utm_*, fbclid, gclid, ref, source)
    - Protocol normalization (http -> https)
    - www. prefix (for consistent matching)

    Returns:
        Canonical URL string
    """
    if not url:
        return url

    # Strip Wayback Machine wrapper
    wayback_pattern = r'https?://web\.archive\.org/web/\d+/'
    url = re.sub(wayback_pattern, '', url)

    # Ensure https
    if url.startswith('http://'):
        url = 'https://' + url[7:]
    if not url.startswith('https://'):
        url = 'https://' + url

    # Strip www. prefix from domain
    url = url.replace('https://www.', 'https://')

    # Strip tracking params
    if '?' in url:
        base, params = url.split('?', 1)
        clean_params = '&'.join(
            p for p in params.split('&')
            if not p.startswith(('utm_', 'fbclid=', 'gclid=', 'ref=', 'source='))
        )
        url = base + ('?' + clean_params if clean_params else '')

    # Strip trailing slash (but not for bare domain)
    if url.count('/') > 3:  # has path beyond domain
        url = url.rstrip('/')

    return url


def extract_domain(url):
    """Extract bare domain from a URL, stripping www. prefix.

    Example: 'https://www.corcoran.com/foo/bar' -> 'corcoran.com'
    """
    return url.split("//")[-1].split("/")[0].replace("www.", "")


def extract_path_keywords(url, depth=2):
    """Extract path segment keywords from a URL for fuzzy matching.

    Filters out short segments (<=3 chars) and returns the last `depth` segments.
    Example: 'https://example.com/news/2026/andrew-kolvet-profile'
             -> ['2026', 'andrew-kolvet-profile']
    """
    path_parts = [pt for pt in url.split("//")[-1].split("/")[1:] if pt and len(pt) > 3]
    return path_parts[-depth:] if len(path_parts) >= depth else path_parts


def fuzzy_match_source(session, url):
    """Resolve a source URL against existing Source nodes in Neo4j.

    Canonicalizes the URL first, then tries exact match (both canonical
    and original forms). If no exact match, falls back to fuzzy matching:
    finds Source nodes on the same domain with archiveStatus='captured'
    and checks if URL path keywords overlap.

    Args:
        session: Active Neo4j session (lifestream or corcoran database)
        url: The source URL to resolve

    Returns:
        Resolved URL string. If an archived Source matches, returns that
        Source's URL. Otherwise returns the canonical URL.
    """
    canonical = canonicalize_url(url)

    # Fast path: exact URL match
    result = session.run(
        "MATCH (s:Source) WHERE s.url IN [$canonical, $original] "
        "RETURN s.url AS url LIMIT 1",
        {"canonical": canonical, "original": url}
    )
    rec = result.single()
    if rec:
        return rec["url"]

    # Fuzzy path: match on domain + path keywords
    domain = extract_domain(canonical)
    keywords = extract_path_keywords(canonical)

    if not keywords:
        return canonical

    fuzzy_result = session.run(
        "MATCH (s:Source) WHERE s.domain = $domain AND s.archiveStatus = 'captured' "
        "RETURN s.url AS url",
        {"domain": domain}
    )
    for record in fuzzy_result:
        candidate = record["url"]
        hits = sum(1 for kw in keywords if kw.lower() in candidate.lower())
        if hits > 0:
            return candidate

    return canonical
