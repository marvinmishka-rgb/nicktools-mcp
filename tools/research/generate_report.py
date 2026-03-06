"""Generate a structured research report from graph data.
---
description: Query the knowledge graph and generate a markdown research report
creates_nodes: []
creates_edges: []
databases: [corcoran, lifestream]
---

Queries the corcoran knowledge graph starting from seed entities, traverses
their connections, gathers evidence, and produces a structured markdown report.

Supports two formats:
- "internal" (default): Full detail with provenance tiers, lifestream refs, property dumps
- "public": Journalist-friendly with timeline-first narrative, numbered footnotes,
  inline source URLs, no internal metadata

Output: Markdown file saved to ClaudeFiles/reports/

Parameters:
    topic (str, required): Report title/topic
    entities (list[str], required): Seed entity names to center the report on
    depth (int, default 2): How many hops to expand from seed entities (1-3)
    format (str, default "internal"): "internal" or "public"
    exclude_labels (list[str], optional): Node labels to exclude from the report
    include_evidence (bool, default True): Include source citations and provenance
    include_timeline (bool, default True): Include chronological events section
    include_gaps (bool, default True): Include section on entities missing evidence
    bundle_archives (bool, default False): Create a zip of the report + archived source files
    output_dir (str, optional): Override output directory
    database (str, default GRAPH_DATABASE): Neo4j graph database to query
"""
import sys
import zipfile
import shutil
from pathlib import Path
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import get_neo4j_driver, execute_read, GRAPH_DATABASE
from lib.paths import CLAUDE_FILES, ensure_dir

DEFAULT_OUTPUT_DIR = str(CLAUDE_FILES / "reports")


# ============================================================
# Query functions (shared by both formats)
# ============================================================

def _query_seed_entities(entity_names, database):
    """Get full node data for seed entities."""
    cypher = """
    UNWIND $names AS name
    MATCH (n {name: name})
    RETURN n.name AS name, labels(n) AS labels, properties(n) AS props
    """
    records, _ = execute_read(cypher, database=database, parameters_={"names": entity_names})
    return [dict(r) for r in records]


def _query_expanded_network(entity_names, depth, database):
    """Expand from seed entities to depth hops, returning all relationships."""
    cypher = """
    UNWIND $names AS seed_name
    MATCH (seed {name: seed_name})
    MATCH path = (seed)-[*1..""" + str(depth) + """]->(connected)
    WHERE connected.name IS NOT NULL
    WITH DISTINCT seed, connected,
         [r IN relationships(path) | {
            type: type(r),
            from: startNode(r).name,
            to: endNode(r).name,
            role: r.role,
            startDate: r.startDate,
            endDate: r.endDate,
            context: r.context,
            source: r.source
         }] AS path_rels
    RETURN seed.name AS seed, connected.name AS connected_name,
           labels(connected) AS connected_labels,
           path_rels
    """
    records, _ = execute_read(cypher, database=database, parameters_={"names": entity_names})
    return [dict(r) for r in records]


def _query_all_relationships(entity_names, database):
    """Get all relationships between any entities in the expanded set."""
    cypher = """
    MATCH (a)-[r]->(b)
    WHERE a.name IN $names AND b.name IN $names
      AND type(r) <> 'SUPPORTED_BY' AND type(r) <> 'DISCUSSES'
      AND type(r) <> 'RESOLVES_TO'
    RETURN a.name AS from_name, labels(a)[0] AS from_label,
           type(r) AS rel_type,
           r.role AS role, r.startDate AS start_date, r.endDate AS end_date,
           r.context AS context, r.source AS source,
           r.compensation AS compensation, r.compensationYear AS comp_year,
           b.name AS to_name, labels(b)[0] AS to_label
    ORDER BY rel_type, from_name
    """
    records, _ = execute_read(cypher, database=database, parameters_={"names": entity_names})
    return [dict(r) for r in records]


def _query_evidence(entity_names, database):
    """Get SUPPORTED_BY edges and Source details for entities."""
    cypher = """
    MATCH (n)-[s:SUPPORTED_BY]->(src:Source)
    WHERE n.name IN $names
    RETURN n.name AS entity,
           src.url AS url, src.domain AS domain, src.title AS title,
           src.archiveStatus AS archive_status, src.sourceType AS source_type,
           src.archivePath AS archive_path,
           s.claim AS claim, s.confidence AS confidence,
           src.capturedAt AS captured_at
    ORDER BY n.name, s.confidence DESC
    """
    records, _ = execute_read(cypher, database=database, parameters_={"names": entity_names})
    return [dict(r) for r in records]


def _query_events(entity_names, database):
    """Get Event nodes connected to the entity set."""
    cypher = """
    MATCH (p)-[r:INVOLVED_IN]->(e:Event)
    WHERE p.name IN $names
    OPTIONAL MATCH (e)-[:SUPPORTED_BY]->(src:Source)
    WITH e, p, r, collect(DISTINCT src.url) AS source_urls
    RETURN e.name AS event, e.date AS date, e.description AS description,
           e.location AS location,
           collect({name: p.name, role: r.role}) AS participants,
           source_urls
    ORDER BY e.date
    """
    records, _ = execute_read(cypher, database=database, parameters_={"names": entity_names})
    return [dict(r) for r in records]


def _query_lifestream_refs(entity_names, database):
    """Get lifestream entries that DISCUSS these entities."""
    cypher = """
    MATCH (ref:EntryRef)-[d:DISCUSSES]->(n)
    WHERE n.name IN $names
    RETURN coalesce(ref.streamEntryId, ref.entryId) AS entry_id,
           ref.title AS title,
           collect(DISTINCT n.name) AS entities_discussed
    ORDER BY entry_id DESC
    """
    records, _ = execute_read(cypher, database=database, parameters_={"names": entity_names})
    return [dict(r) for r in records]


def _query_unsourced_entities(entity_names, database):
    """Find entities in the set with 0 SUPPORTED_BY edges."""
    cypher = """
    UNWIND $names AS name
    MATCH (n {name: name})
    WHERE NOT (n)-[:SUPPORTED_BY]->(:Source)
    RETURN n.name AS name, labels(n) AS labels
    ORDER BY labels(n)[0], n.name
    """
    records, _ = execute_read(cypher, database=database, parameters_={"names": entity_names})
    return [dict(r) for r in records]


# ============================================================
# Event deduplication
# ============================================================

def _deduplicate_events(events):
    """Group events with similar names on same date, or with overlapping participants.
    Keeps the one with more sources (or longer description as tiebreaker)."""
    from difflib import SequenceMatcher
    if not events:
        return events

    def _participants_set(e):
        return set(p["name"] for p in e.get("participants", []) if p.get("name"))

    def _score(e):
        """Higher = better candidate to keep."""
        return (len(e.get("source_urls") or []),
                len(e.get("description") or ""))

    used = set()
    deduped = []
    for i, e1 in enumerate(events):
        if i in used:
            continue
        best = e1
        for j, e2 in enumerate(events):
            if j <= i or j in used:
                continue
            # Same date required
            if not e1.get("date") or e1["date"] != e2.get("date"):
                continue
            # Check name similarity, keyword containment, or overlapping participants
            sim = SequenceMatcher(None, e1["event"], e2["event"]).ratio()
            p1, p2 = _participants_set(e1), _participants_set(e2)
            shared_participants = p1 & p2
            # Keyword containment: extract significant words (3+ chars) and check overlap
            w1 = set(w.lower() for w in e1["event"].split() if len(w) >= 3)
            w2 = set(w.lower() for w in e2["event"].split() if len(w) >= 3)
            keyword_overlap = len(w1 & w2) / max(len(w1), len(w2), 1) if (w1 and w2) else 0
            if (sim > 0.5
                or keyword_overlap > 0.5
                or (shared_participants and len(shared_participants) >= 1 and sim > 0.35)):
                used.add(j)
                if _score(e2) > _score(best):
                    best = e2
        deduped.append(best)
    return deduped


# ============================================================
# Footnote index builder (used by public format)
# ============================================================

class FootnoteIndex:
    """Builds a numbered footnote index from evidence records."""

    def __init__(self, evidence_records):
        self._sources = {}   # canonical_url -> {number, title, domain, url, archive_status, claims}
        self._url_map = {}   # original_url -> canonical_url (for lookups)
        self._next = 1
        for e in evidence_records:
            url = e.get("url")
            if not url:
                continue
            # Canonicalize: strip trailing slash for dedup
            canon = url.rstrip("/")
            self._url_map[url] = canon
            if canon not in self._sources:
                self._sources[canon] = {
                    "number": self._next,
                    "title": e.get("title") or e.get("domain", ""),
                    "domain": e.get("domain", ""),
                    "url": url,
                    "archive_status": e.get("archive_status", ""),
                    "claims": [],
                    "entities": set(),
                }
                self._next += 1
            self._sources[canon]["claims"].append(e.get("claim") or "general")
            self._sources[canon]["entities"].add(e.get("entity", ""))

    def ref(self, url):
        """Return footnote number for a URL, or None if not indexed."""
        canon = self._url_map.get(url, url.rstrip("/"))
        entry = self._sources.get(canon)
        return entry["number"] if entry else None

    def refs_for_entity(self, entity_name):
        """Return sorted footnote numbers for all sources supporting an entity."""
        nums = []
        for s in self._sources.values():
            if entity_name in s["entities"]:
                nums.append(s["number"])
        return sorted(set(nums))

    def render(self):
        """Render the full footnote list as markdown."""
        lines = []
        for s in sorted(self._sources.values(), key=lambda x: x["number"]):
            n = s["number"]
            title = s["title"]
            url = s["url"]
            domain = s["domain"]
            archived = " (archived)" if s["archive_status"] == "captured" else ""
            lines.append(f"[{n}] [{title}]({url}) -- {domain}{archived}")
        return "\n".join(lines)

    @property
    def count(self):
        return len(self._sources)


# ============================================================
# Internal format builders (existing)
# ============================================================

def _format_entity_profile(name, labels, props):
    """Format a single entity's profile as markdown (internal format)."""
    label = labels[0] if labels else "Unknown"
    lines = [f"#### {name} ({label})"]

    skip_keys = {"name", "addedDate", "source"}
    for key, val in sorted(props.items()):
        if key in skip_keys or val is None:
            continue
        if isinstance(val, list):
            if key == "description":
                val = " ".join(str(v) for v in val)
            else:
                val = ", ".join(str(v) for v in val)
        lines.append(f"- **{key}**: {val}")

    return "\n".join(lines)


def _build_relationship_table(relationships):
    """Build a markdown table of relationships (internal format)."""
    if not relationships:
        return "_No relationships found._"

    lines = ["| From | Relationship | To | Role/Context | Period |",
             "|------|-------------|-----|-------------|--------|"]

    for r in relationships:
        from_name = r["from_name"]
        to_name = r["to_name"]
        rel = r["rel_type"].replace("_", " ").title()
        role_ctx = r.get("role") or r.get("context") or ""
        start = r.get("start_date") or ""
        end = r.get("end_date") or ""
        period = f"{start}-{end}" if start or end else ""
        lines.append(f"| {from_name} | {rel} | {to_name} | {role_ctx} | {period} |")

    return "\n".join(lines)


def _build_evidence_section(evidence_records):
    """Build a markdown section with deduplicated sources (internal format).

    Each unique URL is listed once, with all entity references and claims grouped
    beneath it. This avoids the same source appearing repeatedly when it supports
    multiple entities.
    """
    if not evidence_records:
        return "_No archived evidence found._"

    # Group by canonical URL (strip trailing slash for dedup)
    by_url = {}
    url_order = []  # preserve first-seen order
    for e in evidence_records:
        url = (e.get("url") or "").rstrip("/")
        if not url:
            continue
        if url not in by_url:
            by_url[url] = {
                "title": e.get("title") or e.get("domain", "unknown"),
                "domain": e.get("domain", "unknown"),
                "url": e.get("url", ""),
                "archive_status": e.get("archive_status", ""),
                "entities": [],  # (entity, claim, confidence)
            }
            url_order.append(url)
        by_url[url]["entities"].append((
            e["entity"],
            e.get("claim") or "general",
            e.get("confidence", "unknown"),
        ))

    lines = []
    for url in url_order:
        s = by_url[url]
        badge = "[archived]" if s["archive_status"] == "captured" else "[!]" if s["archive_status"] == "failed" else "[--]"
        lines.append(f"#### {badge} [{s['title']}]({s['url']})")
        lines.append(f"*{s['domain']}*")
        lines.append("")
        for entity, claim, confidence in s["entities"]:
            lines.append(f"- **{entity}**: {claim} [{confidence}]")
        lines.append("")

    return "\n".join(lines)


def _build_timeline(events):
    """Build a chronological timeline section (internal format)."""
    if not events:
        return "_No dated events found._"

    lines = []
    for e in events:
        date = e.get("date") or "undated"
        name = e["event"]
        desc = e.get("description") or ""
        location = e.get("location") or ""
        participants = e.get("participants", [])
        participant_names = [p["name"] for p in participants if p.get("name")]

        lines.append(f"- **{date}** -- {name}")
        if desc:
            lines.append(f"  {desc}")
        if location:
            lines.append(f"  Location: {location}")
        if participant_names:
            lines.append(f"  Participants: {', '.join(participant_names)}")
        lines.append("")

    return "\n".join(lines)


# ============================================================
# Public format builders
# ============================================================

def _public_entity_profile(name, labels, props, footnotes, relationships):
    """Format a single entity's profile for public consumption.

    Includes key facts with footnote refs and relationship context woven in.
    """
    label = labels[0] if labels else "Unknown"
    lines = [f"### {name}"]

    # Build a concise description from props
    desc = props.get("description", "")
    if isinstance(desc, list):
        desc = " ".join(str(d) for d in desc)
    if desc:
        refs = footnotes.refs_for_entity(name)
        ref_str = " " + ", ".join(f"[{n}]" for n in refs[:3]) if refs else ""
        lines.append(f"{desc}{ref_str}")
        lines.append("")

    # Key structured info
    info_parts = []
    if "roles" in props and props["roles"]:
        roles = props["roles"] if isinstance(props["roles"], list) else [props["roles"]]
        info_parts.append(f"**Roles**: {', '.join(str(r) for r in roles)}")
    if "ghostCohort" in props and props["ghostCohort"]:
        info_parts.append("**Note**: Identified as part of the Ghost Cohort")
    if "altNames" in props and props["altNames"]:
        alts = props["altNames"] if isinstance(props["altNames"], list) else [props["altNames"]]
        info_parts.append(f"**Also known as**: {', '.join(str(a) for a in alts)}")

    if info_parts:
        for part in info_parts:
            lines.append(f"- {part}")
        lines.append("")

    # Weave in key relationships as prose
    entity_rels = [r for r in relationships
                   if r["from_name"] == name or r["to_name"] == name]

    # Group by type for readable output
    employment = [r for r in entity_rels if r["rel_type"] in ("EMPLOYED_BY", "WORKED_AT")]
    affiliations = [r for r in entity_rels if r["rel_type"] == "AFFILIATED_WITH"]
    family = [r for r in entity_rels if r["rel_type"] == "FAMILY_OF"]
    collaborations = [r for r in entity_rels if r["rel_type"] == "COLLABORATED_WITH"]

    if employment:
        emp_strs = []
        seen_emp = set()  # deduplicate by (org, role)
        for r in employment:
            org = r["to_name"] if r["from_name"] == name else r["from_name"]
            role = r.get("role") or ""
            if isinstance(role, list):
                role = ", ".join(str(x) for x in role)
            dedup_key = (org, role)
            if dedup_key in seen_emp:
                continue
            seen_emp.add(dedup_key)
            period = ""
            if r.get("start_date") or r.get("end_date"):
                s = r.get("start_date") or "?"
                e = r.get("end_date") or "present"
                period = f" ({s}-{e})"
            comp = ""
            if r.get("compensation"):
                comp = f", ${r['compensation']:,}" if isinstance(r['compensation'], (int, float)) else ""
            emp_strs.append(f"{org}{' -- ' + role if role else ''}{period}{comp}")
        lines.append(f"**Employment**: {'; '.join(emp_strs)}")
        lines.append("")

    if affiliations:
        aff_strs = []
        seen_aff = set()
        for r in affiliations:
            org = r["to_name"] if r["from_name"] == name else r["from_name"]
            role = r.get("role") or ""
            if isinstance(role, list):
                role = ", ".join(str(x) for x in role)
            dedup_key = (org, role)
            if dedup_key in seen_aff:
                continue
            seen_aff.add(dedup_key)
            period = ""
            if r.get("start_date") or r.get("end_date"):
                s = r.get("start_date") or "?"
                e = r.get("end_date") or "present"
                period = f" ({s}-{e})"
            aff_strs.append(f"{org}{' -- ' + role if role else ''}{period}")
        lines.append(f"**Affiliations**: {'; '.join(aff_strs)}")
        lines.append("")

    if family:
        fam_strs = []
        seen_family = set()
        for r in family:
            other = r["to_name"] if r["from_name"] == name else r["from_name"]
            if other in seen_family:
                continue
            seen_family.add(other)
            rel = r.get("role") or r.get("context") or ""
            if isinstance(rel, list):
                rel = ", ".join(str(x) for x in rel)
            fam_strs.append(f"{other}{' (' + rel + ')' if rel else ''}")
        lines.append(f"**Family connections**: {'; '.join(fam_strs)}")
        lines.append("")

    if collaborations:
        for r in collaborations:
            other = r["to_name"] if r["from_name"] == name else r["from_name"]
            ctx = r.get("context") or ""
            if isinstance(ctx, list):
                ctx = "; ".join(str(x) for x in ctx)
            if ctx:
                lines.append(f"**Connection to {other}**: {ctx}")
                lines.append("")

    return "\n".join(lines)


def _public_timeline(events, footnotes):
    """Build a narrative timeline for the public format with footnote refs."""
    if not events:
        return "_No dated events found._"

    lines = []
    for e in events:
        date = e.get("date") or "undated"
        name = e["event"]
        desc = e.get("description") or ""
        location = e.get("location") or ""
        participants = e.get("participants", [])
        source_urls = e.get("source_urls") or []

        # Build footnote references for this event
        refs = []
        for url in source_urls:
            n = footnotes.ref(url)
            if n:
                refs.append(f"[{n}]")
        ref_str = " " + ", ".join(refs) if refs else ""

        participant_names = [p["name"] for p in participants if p.get("name")]
        people_str = f" ({', '.join(participant_names)})" if participant_names else ""

        lines.append(f"**{date}** -- **{name}**{people_str}{ref_str}")
        if desc:
            lines.append(f"  {desc}")
        if location:
            lines.append(f"  *Location: {location}*")
        lines.append("")

    return "\n".join(lines)


# ============================================================
# Archive zip bundler
# ============================================================

def _bundle_archives(report_path, evidence_records, out_dir):
    """Create a zip of the report markdown + archived source files.

    Returns zip file path and count of bundled files.
    """
    zip_name = report_path.stem + "_bundle.zip"
    zip_path = out_dir / zip_name
    bundled = 0
    seen_paths = set()

    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Add the report itself
        zf.write(report_path, f"report/{report_path.name}")

        # Add archived source files
        for e in evidence_records:
            archive_path = e.get("archive_path")
            if not archive_path or archive_path in seen_paths:
                continue
            seen_paths.add(archive_path)

            p = Path(archive_path)
            if not p.exists():
                continue

            # Organize by domain in the zip
            domain = e.get("domain", "unknown")
            arc_name = f"sources/{domain}/{p.name}"
            zf.write(p, arc_name)
            bundled += 1

            # Also include .html if the .txt exists (or vice versa)
            for sibling_ext in [".html", ".txt", ".pdf"]:
                sibling = p.with_suffix(sibling_ext)
                if sibling.exists() and str(sibling) not in seen_paths:
                    seen_paths.add(str(sibling))
                    zf.write(sibling, f"sources/{domain}/{sibling.name}")
                    bundled += 1

    return zip_path, bundled


# ============================================================
# Main report generator
# ============================================================

def generate_report_impl(topic, entities, depth=2, format="internal",
                          exclude_labels=None, include_evidence=True,
                          include_timeline=True, include_gaps=True,
                          bundle_archives=False,
                          output_dir=None, database=GRAPH_DATABASE, driver=None, **kwargs):
    """Generate a structured markdown research report from graph data.

    Args:
        topic: Report title
        entities: List of seed entity names
        depth: Expansion depth (1-3)
        format: "internal" (full detail) or "public" (journalist-friendly)
        exclude_labels: List of node labels to exclude from the report
        include_evidence: Include source citations
        include_timeline: Include chronological events
        include_gaps: Include section on unsourced entities
        bundle_archives: Create a zip of report + archived source files
        output_dir: Override output directory
        database: Neo4j database
        driver: Shared Neo4j driver (injected)

    Returns:
        dict with file_path, entity_count, relationship_count, source_count
    """
    if not topic:
        return {"error": "Missing required parameter 'topic'"}
    if not entities:
        return {"error": "Missing required parameter 'entities' (list of entity names)"}

    is_public = (format == "public")
    depth = max(1, min(depth, 3))
    exclude_labels = set(exclude_labels or [])
    out_dir = Path(output_dir) if output_dir else Path(DEFAULT_OUTPUT_DIR)
    ensure_dir(out_dir, "report output directory")

    # Always-excluded structural labels
    structural_labels = {"Source", "EntryRef", "Market", "Region", "Neighborhood"}
    exclude_labels = exclude_labels | structural_labels

    # ---- Phase 1: Query graph data ----

    seeds = _query_seed_entities(entities, database)
    seed_names = set(e["name"] for e in seeds)

    expanded = _query_expanded_network(entities, depth, database)
    all_names = set(entities)
    for row in expanded:
        all_names.add(row["seed"])
        all_names.add(row["connected_name"])
    all_names_list = sorted(all_names)

    relationships = _query_all_relationships(all_names_list, database)

    evidence = []
    if include_evidence:
        evidence = _query_evidence(all_names_list, database)

    events = []
    if include_timeline:
        events = _query_events(all_names_list, database)
        # Filter events: keep only those where at least one seed entity is a participant
        if is_public:
            events = [e for e in events
                       if any(p["name"] in seed_names
                              for p in e.get("participants", []) if p.get("name"))]
        events = _deduplicate_events(events)

    ls_refs = []
    if not is_public:
        ls_refs = _query_lifestream_refs(all_names_list, database)

    unsourced = []
    if include_gaps:
        unsourced = _query_unsourced_entities(all_names_list, database)

    # ---- Phase 2: Entity details, filtered by exclude_labels ----

    entity_details = _query_seed_entities(all_names_list, database)

    entities_by_label = defaultdict(list)
    for e in entity_details:
        primary_label = e["labels"][0] if e["labels"] else "Unknown"
        if primary_label in exclude_labels:
            continue
        entities_by_label[primary_label].append(e)

    # Build filtered name set for the report (after label exclusion)
    included_names = set()
    for label_entities in entities_by_label.values():
        for e in label_entities:
            included_names.add(e["name"])

    # Filter relationships to only include entities in the report
    relationships = [r for r in relationships
                     if r["from_name"] in included_names and r["to_name"] in included_names]

    # Filter unsourced similarly
    unsourced = [u for u in unsourced
                 if u.get("labels") and u["labels"][0] not in exclude_labels]

    # For public format, also filter unsourced events to only those relevant to seeds
    if is_public:
        # Get event names that survived our seed-participant filter
        kept_event_names = set(e["event"] for e in events) if events else set()
        unsourced = [u for u in unsourced
                     if u.get("labels", [None])[0] != "Event"
                     or u["name"] in kept_event_names]

    # ---- Phase 3: Build report ----

    report_date = datetime.now().strftime("%Y-%m-%d")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if is_public:
        report_content = _build_public_report(
            topic, entities, report_date, depth,
            entities_by_label, seed_names, included_names,
            relationships, evidence, events, unsourced
        )
    else:
        report_content = _build_internal_report(
            topic, entities, report_date, depth,
            entities_by_label, seed_names, all_names, included_names,
            relationships, evidence, events, unsourced, ls_refs,
            include_evidence, include_timeline, include_gaps
        )

    # ---- Phase 4: Write file ----

    safe_topic = "".join(c if c.isalnum() or c in " -_" else "" for c in topic)
    safe_topic = safe_topic.strip().replace(" ", "-").lower()[:60]
    fmt_tag = "-public" if is_public else ""
    filename = f"{safe_topic}{fmt_tag}_{timestamp}.md"
    file_path = out_dir / filename

    file_path.write_text(report_content, encoding="utf-8")

    result = {
        "file_path": str(file_path),
        "filename": filename,
        "topic": topic,
        "format": format,
        "entity_count": len(included_names),
        "relationship_count": len(relationships),
        "source_count": len(set(e["url"] for e in evidence if e.get("url"))) if evidence else 0,
        "event_count": len(events),
        "unsourced_count": len(unsourced),
    }

    # ---- Phase 5: Optional archive bundle ----

    if bundle_archives and evidence:
        zip_path, bundled_count = _bundle_archives(file_path, evidence, out_dir)
        result["bundle_path"] = str(zip_path)
        result["bundle_file_count"] = bundled_count
        result["bundle_size_mb"] = round(zip_path.stat().st_size / (1024 * 1024), 2)

    return result


# ============================================================
# Internal format report builder
# ============================================================

def _build_internal_report(topic, entities, report_date, depth,
                           entities_by_label, seed_names, all_names, included_names,
                           relationships, evidence, events, unsourced, ls_refs,
                           include_evidence, include_timeline, include_gaps):
    """Build the internal-format markdown report (existing behavior)."""

    sections = []

    # Header
    sections.append(f"# {topic}")
    sections.append("")
    sections.append(f"*Generated {report_date} from the Corcoran Knowledge Graph*")
    sections.append(f"*Seed entities: {', '.join(entities)}*")
    sections.append(f"*Network depth: {depth} hops -- {len(included_names)} entities, {len(relationships)} relationships*")
    sections.append("")
    sections.append("---")
    sections.append("")

    # Table of Contents
    sections.append("## Table of Contents")
    sections.append("")
    sec_num = 1
    sections.append(f"{sec_num}. [Entity Profiles](#entity-profiles)")
    sec_num += 1
    sections.append(f"{sec_num}. [Relationship Map](#relationship-map)")
    if include_timeline and events:
        sec_num += 1
        sections.append(f"{sec_num}. [Timeline](#timeline)")
    if include_evidence and evidence:
        sec_num += 1
        sections.append(f"{sec_num}. [Evidence & Sources](#evidence--sources)")
    if include_gaps and unsourced:
        sec_num += 1
        sections.append(f"{sec_num}. [Gaps & Open Questions](#gaps--open-questions)")
    if ls_refs:
        sec_num += 1
        sections.append(f"{sec_num}. [Related Analysis](#related-analysis)")
    sections.append("")
    sections.append("---")
    sections.append("")

    # Entity Profiles by type
    sections.append("## Entity Profiles")
    sections.append("")

    label_order = ["Person", "Organization", "Event", "Document", "Property"]
    seen_labels = set()

    for label in label_order:
        if label in entities_by_label:
            seen_labels.add(label)
            entities_list = entities_by_label[label]
            entities_list.sort(key=lambda e: (0 if e["name"] in seed_names else 1, e["name"]))
            sections.append(f"### {label}s")
            sections.append("")
            for e in entities_list:
                sections.append(_format_entity_profile(e["name"], e["labels"], e["props"]))
                sections.append("")

    for label, entities_list in sorted(entities_by_label.items()):
        if label not in seen_labels:
            entities_list.sort(key=lambda e: e["name"])
            sections.append(f"### {label}s")
            sections.append("")
            for e in entities_list:
                sections.append(_format_entity_profile(e["name"], e["labels"], e["props"]))
                sections.append("")

    sections.append("---")
    sections.append("")

    # Relationship Map
    sections.append("## Relationship Map")
    sections.append("")
    sections.append(_build_relationship_table(relationships))
    sections.append("")
    sections.append("---")
    sections.append("")

    # Timeline
    if include_timeline and events:
        sections.append("## Timeline")
        sections.append("")
        sections.append(_build_timeline(events))
        sections.append("---")
        sections.append("")

    # Evidence
    if include_evidence and evidence:
        sections.append("## Evidence & Sources")
        sections.append("")
        unique_sources = set(e["url"] for e in evidence if e.get("url"))
        sections.append(f"*{len(evidence)} evidence edges across {len(unique_sources)} unique sources*")
        sections.append("")
        sections.append("Legend: [archived] = archived, [!] = archive failed, [--] = not archived")
        sections.append("")
        sections.append(_build_evidence_section(evidence))
        sections.append("---")
        sections.append("")

    # Gaps
    if include_gaps and unsourced:
        sections.append("## Gaps & Open Questions")
        sections.append("")
        sections.append(f"**{len(unsourced)} entities have no archived evidence:**")
        sections.append("")
        for u in unsourced:
            label = u["labels"][0] if u["labels"] else "?"
            sections.append(f"- {u['name']} ({label})")
        sections.append("")
        sections.append("---")
        sections.append("")

    # Related Analysis (lifestream entries)
    if ls_refs:
        sections.append("## Related Analysis")
        sections.append("")
        sections.append("Lifestream entries that discuss entities in this report:")
        sections.append("")
        for ref in ls_refs:
            entry_id = ref.get("entry_id", "?")
            title = ref.get("title", "untitled")
            discussed = ref.get("entities_discussed", [])
            sections.append(f"- **{entry_id}**: {title}")
            if discussed:
                sections.append(f"  Entities: {', '.join(discussed[:8])}" +
                              (f" +{len(discussed)-8} more" if len(discussed) > 8 else ""))
        sections.append("")
        sections.append("---")
        sections.append("")

    # Footer
    sections.append("## Methodology")
    sections.append("")
    sections.append(f"This report was generated from the Corcoran Knowledge Graph on {report_date}.")
    sections.append(f"Starting from {len(entities)} seed entities, the graph was traversed to depth {depth}.")
    sections.append(f"The resulting network contains {len(included_names)} entities and {len(relationships)} relationships.")
    if evidence:
        unique_sources = set(e["url"] for e in evidence if e.get("url"))
        unique_domains = set(e.get("domain") for e in evidence if e.get("domain"))
        sections.append(f"Evidence is drawn from {len(unique_sources)} unique sources across {len(unique_domains)} domains.")
    sections.append("")
    sections.append("Evidence quality tiers:")
    sections.append("- **archived-verified**: Source page captured and archived locally")
    sections.append("- **web-search**: Found via web search but not archived")
    sections.append("- **training-knowledge**: From the LLM's training data (unverified)")
    sections.append("- **hearsay**: Secondhand claim, not independently verified")
    sections.append("")

    return "\n".join(sections)


# ============================================================
# Public format report builder
# ============================================================

def _build_public_report(topic, entities, report_date, depth,
                          entities_by_label, seed_names, included_names,
                          relationships, evidence, events, unsourced):
    """Build the public/journalist-friendly format report.

    Structure: Title -> Summary -> Timeline -> Key People -> Key Organizations ->
    Relationship Map -> Source Index -> Gaps (if any)
    """
    footnotes = FootnoteIndex(evidence)
    sections = []

    # Header
    sections.append(f"# {topic}")
    sections.append("")
    sections.append(f"*Preliminary research document -- {report_date}*")
    sections.append(f"*{len(included_names)} entities, {len(relationships)} documented connections, {footnotes.count} sources*")
    sections.append("")
    sections.append("---")
    sections.append("")

    # Summary paragraph
    person_count = len(entities_by_label.get("Person", []))
    org_count = len(entities_by_label.get("Organization", []))
    event_count = len(events)
    sections.append("## Overview")
    sections.append("")
    sections.append(
        f"This document summarizes research on {', '.join(entities)}, "
        f"covering {person_count} individuals, {org_count} organizations, "
        f"and {event_count} documented events. "
        f"Numbered references in brackets (e.g., [1]) correspond to sources listed in the Source Index at the end of this document."
    )
    if unsourced:
        sections.append(
            f" {len(unsourced)} entities currently lack independent sourcing and are flagged in the Gaps section."
        )
    sections.append("")
    sections.append("---")
    sections.append("")

    # Timeline (FIRST in public format -- this is the narrative backbone)
    if events:
        sections.append("## Timeline")
        sections.append("")
        sections.append(_public_timeline(events, footnotes))
        sections.append("---")
        sections.append("")

    # Key People
    people = entities_by_label.get("Person", [])
    if people:
        sections.append("## Key People")
        sections.append("")
        people.sort(key=lambda e: (0 if e["name"] in seed_names else 1, e["name"]))
        for e in people:
            sections.append(_public_entity_profile(
                e["name"], e["labels"], e["props"], footnotes, relationships
            ))
            sections.append("")

        sections.append("---")
        sections.append("")

    # Key Organizations
    orgs = entities_by_label.get("Organization", [])
    if orgs:
        sections.append("## Key Organizations")
        sections.append("")
        orgs.sort(key=lambda e: (0 if e["name"] in seed_names else 1, e["name"]))
        for e in orgs:
            sections.append(_public_entity_profile(
                e["name"], e["labels"], e["props"], footnotes, relationships
            ))
            sections.append("")

        sections.append("---")
        sections.append("")

    # Relationship Map (compact version for public)
    if relationships:
        sections.append("## Connections")
        sections.append("")
        sections.append(_build_relationship_table(relationships))
        sections.append("")
        sections.append("---")
        sections.append("")

    # Gaps
    if unsourced:
        sections.append("## Gaps & Areas for Further Investigation")
        sections.append("")
        sections.append(f"The following {len(unsourced)} entities appear in the research but currently lack independent source documentation:")
        sections.append("")
        for u in unsourced:
            label = u["labels"][0] if u["labels"] else "?"
            sections.append(f"- {u['name']} ({label})")
        sections.append("")
        sections.append("---")
        sections.append("")

    # Source Index (footnotes at the end)
    sections.append("## Source Index")
    sections.append("")
    if footnotes.count > 0:
        sections.append(footnotes.render())
    else:
        sections.append("_No sources indexed._")
    sections.append("")
    sections.append("---")
    sections.append("")

    # Methodology (brief for public)
    sections.append("## About This Document")
    sections.append("")
    sections.append(
        f"This preliminary research document was generated on {report_date} from a structured knowledge graph. "
        f"It covers {len(included_names)} entities connected within {depth} degrees of the seed subjects "
        f"({', '.join(entities)}). "
        f"Sources marked \"(archived)\" have been captured and preserved locally. "
        f"This document is shared as a research aid -- claims should be independently verified before publication."
    )
    sections.append("")

    return "\n".join(sections)


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = generate_report_impl(**params)
    output(result)
