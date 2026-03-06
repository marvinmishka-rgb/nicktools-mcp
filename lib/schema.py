from lib.db import GRAPH_DATABASE
"""
Layer 0 -- Graph schema registry.

No internal dependencies. Defines valid node types, relationship types,
merge keys, required/optional properties, and validation functions.

The registry is the authoritative starting point for all graph operations.
sync_schema_from_neo4j() extends it additively from live data via apoc.meta.schema.
"""
from urllib.parse import urlparse

# ============================================================
# Node Type Registry
# ============================================================
# Each node type defines:
#   merge_key: str or list[str] -- property(s) used in MERGE clause
#   required: list[str] -- properties that MUST be provided
#   optional: list[str] -- known properties (validated but not required)
#   auto_set: dict -- properties auto-set ON CREATE (Cypher expressions as strings)
#   extra_labels: bool -- whether dynamic labels are allowed
#   extra_props: bool -- whether arbitrary additional properties are allowed

NODE_TYPES = {
    "Person": {
        "merge_key": "name",
        "required": ["name"],
        "optional": ["description", "source", "addedDate", "roles"],
        "auto_set": {"addedDate": "date()"},
        "extra_labels": True,
        "extra_props": True,
    },
    "Agent": {
        "merge_key": "name",
        "required": ["name"],
        "optional": ["role", "office", "licenseNumber", "education", "languages",
                     "specialties", "currentFirm", "salesRecord", "priorExperience",
                     "markets", "affiliations", "verifiedDate", "ghostCohort",
                     "verificationNotes", "agentIds"],
        "auto_set": {"addedDate": "date()"},
        "extra_labels": False,
        "extra_props": True,
    },
    "Organization": {
        "merge_key": "name",
        "required": ["name"],
        "optional": ["type", "description", "source", "addedDate", "founded",
                     "headquarters", "website"],
        "auto_set": {"addedDate": "date()"},
        "extra_labels": False,
        "extra_props": True,
    },
    "Event": {
        "merge_key": "name",
        "required": ["name"],
        "optional": ["type", "date", "description", "source", "location"],
        "auto_set": {"addedDate": "date()"},
        "extra_labels": False,
        "extra_props": True,
    },
    "Document": {
        "merge_key": ["name", "docType"],
        "required": ["name"],
        "optional": ["docType", "date", "author", "description", "source",
                     "archivePath", "pageCount", "filedBy"],
        "auto_set": {"addedDate": "date()"},
        "extra_labels": False,
        "extra_props": True,
    },
    "Property": {
        "merge_key": "address",
        "required": ["address"],
        "optional": ["city", "state", "zip", "propertyType", "fullAddress",
                     "description", "source", "parcelId", "currentValue"],
        "auto_set": {"addedDate": "date()"},
        "extra_labels": False,
        "extra_props": True,
    },
    "Source": {
        "merge_key": "url",
        "required": ["url"],
        "optional": ["domain", "title", "sourceType", "capturedAt", "archiveStatus",
                     "textPreview", "textSize", "archivePath", "author",
                     "publishedDate", "siteName", "failureReason"],
        "auto_set": {},
        "extra_labels": False,
        "extra_props": False,
    },
    # --- Geographic taxonomy (read-only fixtures from agent data) ---
    "Neighborhood": {
        "merge_key": "name",
        "required": ["name"],
        "optional": [],
        "auto_set": {},
        "extra_labels": False,
        "extra_props": False,
    },
    "Market": {
        "merge_key": "name",
        "required": ["name"],
        "optional": [],
        "auto_set": {},
        "extra_labels": False,
        "extra_props": False,
    },
    "Region": {
        "merge_key": "regionId",
        "required": ["regionId"],
        "optional": ["marketName"],
        "auto_set": {},
        "extra_labels": False,
        "extra_props": False,
    },
    # --- Organization subtypes (distinct labels used in relationships) ---
    "Brokerage": {
        "merge_key": "name",
        "required": ["name"],
        "optional": ["type", "shortName", "address", "parentCompany",
                     "lifestreamRefs", "keyPersonnel", "commissionStructure",
                     "contractTerms", "officeOpenedNYC",
                     "nyscefCase", "caseTitle", "caseStatus", "caseFilingDate",
                     "caseSettlementDate", "caseSummary", "courtDocuments", "documentFiles"],
        "auto_set": {"addedDate": "date()"},
        "extra_labels": False,
        "extra_props": True,
    },
    "Team": {
        "merge_key": "name",
        "required": ["name"],
        "optional": ["awards", "website", "status"],
        "auto_set": {"addedDate": "date()"},
        "extra_labels": False,
        "extra_props": True,
    },
    "LawFirm": {
        "merge_key": "name",
        "required": ["name"],
        "optional": ["type", "address", "phone", "fax", "specialty", "source",
                     "nyscefCase"],
        "auto_set": {"addedDate": "date()"},
        "extra_labels": False,
        "extra_props": True,
    },
}


# ============================================================
# Label Categorization
# ============================================================
# Research entities require SUPPORTED_BY edges and full audit scrutiny.
# Fixture entities are bulk-imported infrastructure (e.g. agent data)
# that don't need individual sourcing.

RESEARCH_LABELS = {"Person", "Organization", "Event", "Document", "Property",
                   "Brokerage", "Team", "LawFirm"}

FIXTURE_LABELS = {"Agent", "Neighborhood", "Market", "Region"}


def is_research_label(label):
    """Check if a label is a research entity (needs full audit scrutiny)."""
    return label in RESEARCH_LABELS


def is_fixture_label(label):
    """Check if a label is a fixture/infrastructure entity."""
    return label in FIXTURE_LABELS


# ============================================================
# Source Domain Classification
# ============================================================
# Maps normalized domains to sourceType values for bulk classification.
# Domain keys should be bare (no www. prefix) — classify_source_by_domain()
# strips www. before lookup.

SOURCE_DOMAIN_MAP = {
    # --- Primary journalism ---
    "nytimes.com": "primary-journalism",
    "washingtonpost.com": "primary-journalism",
    "wsj.com": "primary-journalism",
    "therealdeal.com": "primary-journalism",
    "newyorker.com": "primary-journalism",
    "npr.org": "primary-journalism",
    "nbcnews.com": "primary-journalism",
    "chicagotribune.com": "primary-journalism",
    "chicagobusiness.com": "primary-journalism",
    "foxnews.com": "primary-journalism",
    "cnn.com": "primary-journalism",
    "abcnews.com": "primary-journalism",
    "huffpost.com": "primary-journalism",
    "newsweek.com": "primary-journalism",
    "fortune.com": "primary-journalism",
    "thedailybeast.com": "primary-journalism",
    "hollywoodreporter.com": "primary-journalism",
    "nbcchicago.com": "primary-journalism",
    "propublica.org": "primary-journalism",
    "12news.com": "primary-journalism",
    "rawstory.com": "primary-journalism",
    "notus.org": "primary-journalism",
    "floodlightnews.org": "primary-journalism",
    "religionnews.com": "primary-journalism",
    "baptistnews.com": "primary-journalism",
    "patch.com": "primary-journalism",
    "housingwire.com": "primary-journalism",
    # Regional journalism
    "sltrib.com": "primary-journalism",
    "azcapitoltimes.com": "primary-journalism",
    "azmirror.com": "primary-journalism",
    "phoenixnewtimes.com": "primary-journalism",
    "kjzz.org": "primary-journalism",
    "kawc.org": "primary-journalism",
    "kuer.org": "primary-journalism",
    "ktar.com": "primary-journalism",
    "kold.com": "primary-journalism",
    "fox13now.com": "primary-journalism",
    "statepress.com": "primary-journalism",
    "deseret.com": "primary-journalism",
    "utahnewsdispatch.com": "primary-journalism",
    "billingsgazette.com": "primary-journalism",
    "bismarcktribune.com": "primary-journalism",
    "gazette.com": "primary-journalism",
    "golocalprov.com": "primary-journalism",
    "arizonafoothillsmagazine.com": "primary-journalism",
    "yourvalley.net": "primary-journalism",
    "sonorannews.com": "primary-journalism",
    "armenianweekly.com": "primary-journalism",
    # --- Encyclopedic ---
    "wikipedia.org": "encyclopedic",
    "en.wikipedia.org": "encyclopedic",
    "britannica.com": "encyclopedic",
    # --- Public records ---
    "sec.gov": "public-record",
    "dos.ny.gov": "public-record",
    "irs.gov": "public-record",
    "ecorp.azcc.gov": "public-record",
    "mcassessor.maricopa.gov": "public-record",
    "atty.utahcounty.gov": "public-record",
    "usaspending.gov": "public-record",
    "opencorporates.com": "public-record",
    "patents.justia.com": "public-record",
    "patents.google.com": "public-record",
    "law.justia.com": "public-record",
    "projects.propublica.org": "public-record",
    "governmentcontractswon.com": "public-record",
    "govtribe.com": "public-record",
    "bizapedia.com": "public-record",
    "corporationwiki.com": "public-record",
    # --- Investigative / advocacy research ---
    "politicalresearch.org": "advocacy-research",
    "influencewatch.org": "advocacy-research",
    "sourcewatch.org": "advocacy-research",
    "mediamatters.org": "advocacy-research",
    "ministrywatch.com": "advocacy-research",
    # --- Partisan media ---
    "breitbart.com": "partisan-media",
    "washingtontimes.com": "partisan-media",
    "humanevents.com": "partisan-media",
    "headlineusa.com": "partisan-media",
    "americanthinker.com": "partisan-media",
    "razorwirenews.com": "partisan-media",
    # --- Tabloid / entertainment ---
    "nickiswift.com": "tabloid",
    "thelist.com": "tabloid",
    "primetimer.com": "tabloid",
    "meaww.com": "tabloid",
    "thecinemaholic.com": "tabloid",
    "famousbirthdays.com": "tabloid",
    # --- Industry / real estate ---
    "compass.com": "industry-source",
    "streeteasy.com": "industry-source",
    "zillow.com": "industry-source",
    "realestatenews.com": "industry-source",
    "newyorkyimby.com": "industry-source",
    "homes.com": "industry-source",
    "homesandland.com": "industry-source",
    "loopnet.com": "industry-source",
    "commercialcafe.com": "industry-source",
    "offthemrkt.com": "industry-source",
    "triplemint.com": "industry-source",
    "avisonyoung.com": "industry-source",
    "mannpublications.com": "industry-source",
    # --- AI-generated content ---
    "factually.co": "ai-generated",
    "grokipedia.com": "ai-generated",
    # --- News aggregation ---
    "yahoo.com": "news-aggregation",
    "finance.yahoo.com": "news-aggregation",
    "benzinga.com": "news-aggregation",
    "newswire.com": "news-aggregation",
    "accessnewswire.com": "news-aggregation",
    # --- Blog / factual substrate ---
    "wltreport.com": "blog-factual-substrate",
    "helenaglass.net": "blog-factual-substrate",
    "medium.com": "blog-factual-substrate",
    # --- Fact-checking ---
    "snopes.com": "fact-checking",
    # --- Reference / database ---
    "imdb.com": "reference-database",
    "goodreads.com": "reference-database",
    "bbb.org": "reference-database",
    "findagrave.com": "reference-database",
    "legacy.com": "reference-database",
}


def classify_source_by_domain(url):
    """Return sourceType based on domain, or None if not in the map.

    Strips www. prefix and handles subdomains by checking progressively
    shorter domain parts (e.g. radio.foxnews.com → foxnews.com).

    Args:
        url: Source URL string

    Returns:
        sourceType string or None if domain not recognized
    """
    if not url or not isinstance(url, str):
        return None

    try:
        parsed = urlparse(url if '://' in url else f'https://{url}')
        domain = (parsed.hostname or '').lower()
    except Exception:
        return None

    # Strip www. prefix
    if domain.startswith('www.'):
        domain = domain[4:]

    # Direct lookup
    if domain in SOURCE_DOMAIN_MAP:
        return SOURCE_DOMAIN_MAP[domain]

    # Try progressively shorter subdomains (e.g. radio.foxnews.com → foxnews.com)
    parts = domain.split('.')
    for i in range(1, len(parts) - 1):
        shorter = '.'.join(parts[i:])
        if shorter in SOURCE_DOMAIN_MAP:
            return SOURCE_DOMAIN_MAP[shorter]

    return None


# ============================================================
# Relationship Type Registry
# ============================================================
# Each relationship type defines:
#   from: list[str] -- valid source node labels ("*" = any)
#   to: list[str] -- valid target node labels ("*" = any)
#   props: list[str] -- known property names for this rel type

REL_TYPES = {
    "EMPLOYED_BY":       {"from": ["Person", "Agent"], "to": ["Organization", "Brokerage"],
                          "props": ["role", "startDate", "endDate", "source", "sourceUrl", "provenanceTier"]},
    "WORKED_AT":         {"from": ["Person", "Agent"], "to": ["Organization", "Brokerage"],
                          "props": ["role", "startDate", "endDate", "source", "sourceUrl", "provenanceTier"]},
    "AFFILIATED_WITH":   {"from": ["Person", "Agent"], "to": ["Organization"],
                          "props": ["role", "startDate", "endDate", "context", "source", "sourceUrl", "provenanceTier", "compensation", "compensationYear"]},
    "FAMILY_OF":         {"from": ["Person", "Agent"], "to": ["Person", "Agent"],
                          "props": ["relation", "evidence", "source"]},
    "INVOLVED_IN":       {"from": ["Person", "Agent", "Organization"], "to": ["Event"],
                          "props": ["role", "source"]},
    "SUPPORTED_BY":      {"from": ["*"], "to": ["Source"],
                          "props": ["claim", "confidence", "addedDate", "note", "sourceUrl"]},
    "MEMBER_OF":         {"from": ["Agent"], "to": ["Team"],
                          "props": ["role", "source", "startDate"]},
    "RESOLVES_TO":       {"from": ["Agent"], "to": ["Person"],
                          "props": ["source", "confidence"]},
    "COLLABORATED_WITH": {"from": ["Agent", "Person"], "to": ["Agent", "Person"],
                          "props": ["context", "source"]},
    "PART_OF":           {"from": ["Organization", "Neighborhood", "Market"], "to": ["Organization", "Market", "Region"],
                          "props": ["role", "source", "startDate", "endDate"]},
    "FILED_BY":          {"from": ["Document"], "to": ["Person", "Organization"],
                          "props": ["source"]},
    "MENTIONS":          {"from": ["Document"], "to": ["Person", "Organization", "Event"],
                          "props": ["page", "context"]},
    "OWNED_BY":          {"from": ["Property"], "to": ["Person", "Organization"],
                          "props": ["startDate", "endDate", "price", "salePrice", "current", "source"]},
    "LISTED_BY":         {"from": ["Property"], "to": ["Agent", "Person"],
                          "props": ["brokerage", "date", "price", "status", "source"]},
    "IN_MARKET":         {"from": ["Agent"], "to": ["Market"], "props": []},
    "IN_REGION":         {"from": ["Agent"], "to": ["Region"], "props": []},
    "WORKED_IN":         {"from": ["Agent"], "to": ["Neighborhood"], "props": []},
    "MOVED_TO":          {"from": ["Agent"], "to": ["Organization"], "props": ["date", "source"]},
    "MOVED_FROM":        {"from": ["Agent"], "to": ["Organization"], "props": ["date", "source"]},
    "TEAM_MEMBER_OF":    {"from": ["Agent"], "to": ["Agent"], "props": ["role"]},
    "OCCURRED_AT":       {"from": ["Event"], "to": ["Organization", "Property"],
                          "props": ["source"]},
    "PRESIDED_OVER":     {"from": ["Person"], "to": ["Event"],
                          "props": ["role", "source"]},
    "REPRESENTED":       {"from": ["Person", "Agent"], "to": ["Person", "Organization"],
                          "props": ["role", "context", "source", "startDate", "endDate"]},
    "DISCUSSES":         {"from": ["*"], "to": ["*"],
                          "props": ["entryId"]},
    "RENAMED_TO":        {"from": ["Organization"], "to": ["Organization"],
                          "props": ["date", "source"]},
    "SUCCEEDED_BY":      {"from": ["Person"], "to": ["Person"],
                          "props": ["role", "organization", "date", "source"]},
    "SUED_BY":           {"from": ["Person", "Organization"], "to": ["Person", "Organization"],
                          "props": ["case", "date", "court", "source"]},
    "SUED":              {"from": ["Person", "Organization"], "to": ["Person", "Organization"],
                          "props": ["case", "date", "court", "source"]},
    "DONATED_TO":        {"from": ["Person", "Organization"], "to": ["Person", "Organization"],
                          "props": ["amount", "date", "source"]},
    "LOCATED_IN":        {"from": ["Property", "Organization"], "to": ["*"],
                          "props": ["source"]},
}


# ============================================================
# Validation Functions
# ============================================================

def suggest_label(invalid_label):
    """Return closest valid label(s) by edit distance."""
    from difflib import get_close_matches
    return get_close_matches(invalid_label, list(NODE_TYPES.keys()), n=3, cutoff=0.5)


def suggest_rel_type(invalid_type):
    """Return closest valid rel type(s) by edit distance, plus hardcoded common mistakes."""
    from difflib import get_close_matches

    # Hardcoded suggestions for common conceptual mistakes (not just typos)
    COMMON_MISTAKES = {
        "FOUNDED": "Use AFFILIATED_WITH with role: 'Founder'",
        "BOARD_MEMBER": "Use AFFILIATED_WITH with role: 'Director'",
        "MARRIED": "Use FAMILY_OF with relation: 'spouse'",
        "SPOUSE": "Use FAMILY_OF with relation: 'spouse'",
        "PARENT": "Use FAMILY_OF with relation: 'parent'",
        "CHILD": "Use FAMILY_OF with relation: 'child'",
        "SIBLING": "Use FAMILY_OF with relation: 'sibling'",
        "WORKS_AT": "Use EMPLOYED_BY or WORKED_AT",
        "WORKS_FOR": "Use EMPLOYED_BY",
        "MEMBER_OF_BOARD": "Use AFFILIATED_WITH with role: 'Director'",
        "OWNS": "Use OWNED_BY (reversed direction: property->owner)",
        "CITES": "Use SUPPORTED_BY (reversed direction: entity->source)",
    }

    fuzzy = get_close_matches(invalid_type, list(REL_TYPES.keys()), n=3, cutoff=0.4)
    hardcoded = COMMON_MISTAKES.get(invalid_type)
    return fuzzy, hardcoded


def validate_label(label):
    """Check that label is a known node type. Returns (ok, error_msg)."""
    if label in NODE_TYPES:
        return True, None
    suggestions = suggest_label(label)
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    return False, f"Unknown label '{label}'.{hint} Valid labels: {sorted(NODE_TYPES.keys())}"


def validate_required_props(label, props):
    """Check that all required properties are present. Returns (ok, error_msg)."""
    schema = NODE_TYPES.get(label)
    if not schema:
        return False, f"Unknown label '{label}'"
    missing = [k for k in schema["required"] if k not in props]
    if missing:
        return False, f"Missing required properties for {label}: {missing}"
    return True, None


def validate_props(label, props):
    """Validate properties against the schema. Returns (ok, warnings).

    - Rejects unknown props if extra_props is False
    - Returns warnings for unknown props if extra_props is True
    """
    schema = NODE_TYPES.get(label)
    if not schema:
        return False, [f"Unknown label '{label}'"]

    known = set(schema["required"]) | set(schema["optional"])
    # merge_key fields are also known
    mk = schema["merge_key"]
    if isinstance(mk, list):
        known.update(mk)
    else:
        known.add(mk)

    unknown = set(props.keys()) - known
    if unknown:
        if not schema["extra_props"]:
            return False, [f"Unknown properties for {label} (extra_props=False): {sorted(unknown)}"]
        return True, [f"Extra properties for {label} (not in schema): {sorted(unknown)}"]

    return True, []


def validate_rel_type(rel_type, from_label=None, to_label=None, strict=True):
    """Check relationship type validity and optional from/to label compatibility.

    Args:
        rel_type: Relationship type string (e.g. "EMPLOYED_BY")
        from_label: Optional source node label for compatibility check
        to_label: Optional target node label for compatibility check
        strict: If False, unknown types produce a warning instead of an error.
                Use strict=False during research when novel relationship types
                are expected. The warning is returned as the second element.

    Returns (ok, error_or_warning_msg).
    """
    if rel_type not in REL_TYPES:
        fuzzy, hardcoded = suggest_rel_type(rel_type)
        parts = [f"Unknown relationship type '{rel_type}'."]
        if hardcoded:
            parts.append(f" Hint: {hardcoded}.")
        if fuzzy:
            parts.append(f" Did you mean: {', '.join(fuzzy)}?")

        if not strict:
            # Allow it with a warning -- auto-register for the rest of this session
            REL_TYPES[rel_type] = {
                "from": [from_label] if from_label else ["*"],
                "to": [to_label] if to_label else ["*"],
                "props": [],
            }
            warning = f"New relationship type '{rel_type}' not in registry (auto-registered for this session)."
            if fuzzy:
                warning += f" Similar types exist: {', '.join(fuzzy)}. Verify this isn't a typo."
            if hardcoded:
                warning += f" Note: {hardcoded}"
            return True, warning

        if not hardcoded and not fuzzy:
            parts.append(f" Valid types: {sorted(REL_TYPES.keys())}")
        return False, "".join(parts)

    schema = REL_TYPES[rel_type]

    if from_label and "*" not in schema["from"] and from_label not in schema["from"]:
        return False, (f"{rel_type} cannot originate from {from_label}. "
                       f"Valid from labels: {schema['from']}")

    if to_label and "*" not in schema["to"] and to_label not in schema["to"]:
        return False, (f"{rel_type} cannot target {to_label}. "
                       f"Valid to labels: {schema['to']}")

    return True, None


def get_merge_key(label):
    """Return the merge key(s) for a label. Always returns a list."""
    schema = NODE_TYPES.get(label)
    if not schema:
        return []
    mk = schema["merge_key"]
    return mk if isinstance(mk, list) else [mk]


def get_auto_set(label):
    """Return the auto_set dict for a label (Cypher expressions set ON CREATE)."""
    schema = NODE_TYPES.get(label)
    if not schema:
        return {}
    return schema.get("auto_set", {})


def build_merge_cypher(label, props, action="add"):
    """Build a MERGE/MATCH Cypher query from schema + properties.

    Args:
        label: Node label (e.g. "Agent")
        props: Dict of all properties to set
        action: "add" (MERGE with ON CREATE/ON MATCH) or "update" (MATCH + SET)

    Returns:
        (cypher_string, params_dict) tuple ready for execute_write()
    """
    schema = NODE_TYPES.get(label)
    if not schema:
        raise ValueError(f"Unknown label '{label}'")

    merge_keys = get_merge_key(label)
    auto_set = get_auto_set(label)

    # Build merge key clause: {name: $name} or {address: $address, city: $city, state: $state}
    merge_parts = [f"{k}: ${k}" for k in merge_keys]
    merge_clause = ", ".join(merge_parts)

    # Separate merge-key props from update props
    update_props = {k: v for k, v in props.items() if k not in merge_keys and v is not None}

    # Build params dict
    cypher_params = {}
    for k in merge_keys:
        if k in props:
            cypher_params[k] = props[k]

    if update_props:
        cypher_params["update_props"] = update_props

    if action == "add":
        # MERGE with ON CREATE / ON MATCH
        auto_clauses = ", ".join(f"n.{k} = {v}" for k, v in auto_set.items())
        lines = [f"MERGE (n:{label} {{{merge_clause}}})"]

        # ON CREATE: set auto fields + all provided props
        on_create_parts = []
        if auto_clauses:
            on_create_parts.append(auto_clauses)
        if update_props:
            on_create_parts.append("n += $update_props")
        if on_create_parts:
            lines.append(f"ON CREATE SET {', '.join(on_create_parts)}")

        # ON MATCH: set provided props (not auto fields)
        if update_props:
            lines.append("ON MATCH SET n += $update_props")

        lines.append("RETURN n, labels(n) AS labels")
        cypher = "\n".join(lines)

    elif action == "update":
        # MATCH existing + SET. Fails if node doesn't exist.
        lines = [f"MATCH (n:{label} {{{merge_clause}}})"]
        if update_props:
            lines.append("SET n += $update_props")
        lines.append("RETURN n, labels(n) AS labels")
        cypher = "\n".join(lines)

    elif action == "get":
        # Read-only: return node + immediate relationships
        lines = [
            f"MATCH (n:{label} {{{merge_clause}}})",
            "OPTIONAL MATCH (n)-[r]-(m)",
            "RETURN n, labels(n) AS labels,",
            "       collect(DISTINCT {type: type(r), direction: CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END,",
            "                          target: labels(m)[0] + ': ' + coalesce(m.name, m.title, m.url, m.address, 'unnamed'),",
            "                          props: properties(r)}) AS relationships"
        ]
        cypher = "\n".join(lines)

    else:
        raise ValueError(f"Unknown action '{action}'. Valid: add, update, get")

    return cypher, cypher_params


# ============================================================
# Schema Sync from Live Neo4j
# ============================================================

# Labels that are dynamic extras on Person nodes (via extra_labels: True),
# not standalone entity types. We don't add these to NODE_TYPES.
ROLE_LABELS = {
    "Accountant", "Activist", "Architect", "Attorney", "Author", "Broker",
    "Businessman", "Businesswoman", "Communications", "Consultant", "Donor",
    "Educator", "Engineer", "Entrepreneur", "Executive", "Firefighter",
    "Founder", "Investor", "Judge", "Operative", "Pastor", "Podcaster",
    "PoliticalCommentator", "Politician", "Producer", "Publisher",
    "Spokesperson", "Strategist", "Veteran",
}


def sync_schema_from_neo4j(driver=None, database=GRAPH_DATABASE):
    """Read live schema via APOC and update NODE_TYPES/REL_TYPES additively.

    Primary source (same as official Neo4j MCP Server v1.4.2):
        CALL apoc.meta.schema({sample: 50})

    Discovers:
    - New labels not in NODE_TYPES -> adds with inferred properties
    - New relationship types not in REL_TYPES -> adds with observed from/to pairs
    - Properties observed on existing nodes that aren't in optional/required -> adds to optional

    Does NOT:
    - Remove types/properties from the registry (additive only)
    - Override merge_key or required fields (those are hand-curated)
    - Add role labels (Attorney, Politician, etc.) as standalone types

    Fallback: If APOC is unavailable, uses db.schema.nodeTypeProperties()
    and db.schema.relTypeProperties().

    Args:
        driver: Shared Neo4j driver (creates one if not provided)
        database: Target database (default: corcoran)

    Returns:
        dict with sync results: new_labels, new_rel_types, new_properties, errors
    """
    from lib.db import execute_read, get_neo4j_driver

    _driver = driver or get_neo4j_driver()
    result = {
        "new_labels": [],
        "new_rel_types": [],
        "new_properties": {},  # {label: [new_prop_names]}
        "label_counts": {},
        "errors": [],
        "source": "apoc.meta.schema",
    }

    # Try APOC first, fall back to db.schema procedures
    try:
        records, _ = execute_read(
            "CALL apoc.meta.schema({sample: 50}) YIELD value RETURN value",
            database=database, driver=_driver,
        )
        if not records:
            result["errors"].append("apoc.meta.schema returned no data")
            return result
        schema_map = records[0]["value"]
    except Exception as e:
        result["errors"].append(f"apoc.meta.schema failed: {e}")
        result["source"] = "fallback"
        return _sync_fallback(driver=_driver, database=database, result=result)

    # Process each key in the schema map
    for key, schema_info in schema_map.items():
        schema_type = schema_info.get("type", "")
        count = schema_info.get("count", 0)

        if schema_type == "node":
            _sync_node_label(key, schema_info, result)
        elif schema_type == "relationship":
            _sync_rel_type(key, schema_info, schema_map, result)

    return result


def _sync_node_label(label, schema_info, result):
    """Process a single node label from apoc.meta.schema output."""
    count = schema_info.get("count", 0)
    result["label_counts"][label] = count

    # Skip role labels -- these are dynamic extras on Person, not standalone types
    if label in ROLE_LABELS:
        return

    # Skip EntryRef -- managed by lifestream triggers, not entity operations
    if label == "EntryRef":
        return

    live_props = list(schema_info.get("properties", {}).keys())

    if label in NODE_TYPES:
        # Existing label -- check for new properties
        schema = NODE_TYPES[label]
        known = set(schema["required"]) | set(schema["optional"])
        mk = schema["merge_key"]
        if isinstance(mk, list):
            known.update(mk)
        else:
            known.add(mk)

        new_props = [p for p in live_props if p not in known]
        if new_props:
            schema["optional"].extend(new_props)
            result["new_properties"][label] = new_props
    else:
        # New label -- add to registry with sensible defaults
        NODE_TYPES[label] = {
            "merge_key": "name" if "name" in live_props else live_props[0] if live_props else "name",
            "required": ["name"] if "name" in live_props else [live_props[0]] if live_props else ["name"],
            "optional": [p for p in live_props if p != "name"],
            "auto_set": {"addedDate": "date()"},
            "extra_labels": False,
            "extra_props": True,
        }
        result["new_labels"].append(label)


def _sync_rel_type(rel_type, schema_info, full_schema, result):
    """Process a single relationship type from apoc.meta.schema output."""
    live_props = list(schema_info.get("properties", {}).keys())

    if rel_type in REL_TYPES:
        # Existing rel type -- check for new properties
        known_props = set(REL_TYPES[rel_type]["props"])
        new_props = [p for p in live_props if p not in known_props]
        if new_props:
            REL_TYPES[rel_type]["props"].extend(new_props)
            result["new_properties"][f"rel:{rel_type}"] = new_props
    else:
        # New rel type -- infer from/to by scanning node labels' relationships
        from_labels = []
        to_labels = []
        for label, info in full_schema.items():
            if info.get("type") != "node":
                continue
            rels = info.get("relationships", {})
            if rel_type in rels:
                rel_info = rels[rel_type]
                direction = rel_info.get("direction", "")
                if direction == "out":
                    from_labels.append(label)
                elif direction == "in":
                    to_labels.append(label)

        REL_TYPES[rel_type] = {
            "from": from_labels if from_labels else ["*"],
            "to": to_labels if to_labels else ["*"],
            "props": live_props,
        }
        result["new_rel_types"].append(rel_type)


def _sync_fallback(driver, database, result):
    """Fallback sync using db.schema procedures when APOC is unavailable."""
    from lib.db import execute_read

    try:
        # Node properties
        records, _ = execute_read(
            "CALL db.schema.nodeTypeProperties() YIELD nodeType, propertyName "
            "RETURN nodeType, collect(propertyName) AS props",
            database=database, driver=driver,
        )
        for rec in records:
            label = rec["nodeType"].strip("`").replace(":", "")
            props = rec["props"]
            if label in ROLE_LABELS or label == "EntryRef":
                continue
            if label in NODE_TYPES:
                schema = NODE_TYPES[label]
                known = set(schema["required"]) | set(schema["optional"])
                new_props = [p for p in props if p not in known]
                if new_props:
                    schema["optional"].extend(new_props)
                    result["new_properties"][label] = new_props
            else:
                NODE_TYPES[label] = {
                    "merge_key": "name" if "name" in props else props[0] if props else "name",
                    "required": ["name"] if "name" in props else [props[0]] if props else ["name"],
                    "optional": [p for p in props if p != "name"],
                    "auto_set": {"addedDate": "date()"},
                    "extra_labels": False,
                    "extra_props": True,
                }
                result["new_labels"].append(label)

        # Relationship properties
        records, _ = execute_read(
            "CALL db.schema.relTypeProperties() YIELD relType, propertyName "
            "RETURN relType, collect(propertyName) AS props",
            database=database, driver=driver,
        )
        for rec in records:
            rel_type = rec["relType"].strip("`").replace(":", "")
            props = rec["props"]
            if rel_type in REL_TYPES:
                known_props = set(REL_TYPES[rel_type]["props"])
                new_props = [p for p in props if p not in known_props]
                if new_props:
                    REL_TYPES[rel_type]["props"].extend(new_props)
                    result["new_properties"][f"rel:{rel_type}"] = new_props
            else:
                REL_TYPES[rel_type] = {
                    "from": ["*"], "to": ["*"],
                    "props": props,
                }
                result["new_rel_types"].append(rel_type)

    except Exception as e:
        result["errors"].append(f"Fallback sync failed: {e}")

    return result


def get_sync_summary():
    """Return a summary of current registry state for reference docs.

    Returns:
        dict with node_types, rel_types counts and details
    """
    return {
        "node_types": {
            label: {
                "merge_key": schema["merge_key"],
                "required": schema["required"],
                "optional_count": len(schema["optional"]),
                "extra_props": schema["extra_props"],
            }
            for label, schema in sorted(NODE_TYPES.items())
        },
        "rel_types": {
            rel: {
                "from": schema["from"],
                "to": schema["to"],
                "prop_count": len(schema["props"]),
            }
            for rel, schema in sorted(REL_TYPES.items())
        },
        "role_labels": sorted(ROLE_LABELS),
    }
