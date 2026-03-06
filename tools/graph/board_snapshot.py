"""Board composition snapshot: load a board roster and detect changes.
---
description: Load board members for an organization, create nodes/edges, detect year-over-year changes
creates_nodes: [Person]
creates_edges: [AFFILIATED_WITH]
databases: [corcoran]
optional: true
domain_extension: Organizational research -- tracks board membership over time. Example of extending graph ops for a specific investigation domain. Safe to remove if not needed.
---

Takes a list of board members for an organization and fiscal year, creates
Person nodes and AFFILIATED_WITH edges with temporal properties, and detects
changes (additions, departures, role changes) against the existing board.

The diff compares the new snapshot against existing AFFILIATED_WITH edges
where the role matches board-level patterns and endDate IS NULL (current members).
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.schema import validate_label
from lib.db import execute_read, execute_write, GRAPH_DATABASE
from lib.urls import canonicalize_url


# Roles that indicate board-level positions (used for diff detection)
BOARD_ROLES = {
    'director', 'chairman', 'chairwoman', 'chair', 'vice chair', 'vice chairman',
    'treasurer', 'secretary', 'ceo', 'coo', 'cfo', 'president', 'vice president',
    'officer', 'trustee', 'board member', 'executive director',
}


def _normalize_role(role):
    """Normalize role string for comparison."""
    if not role:
        return ""
    return role.strip().lower()


def _is_board_role(role):
    """Check if a role string matches board-level patterns."""
    normalized = _normalize_role(role)
    # Check each board role as a substring (handles compound roles like "CEO & Chairwoman")
    for br in BOARD_ROLES:
        if br in normalized:
            return True
    return False


def _get_existing_board(org_name, database, driver):
    """Query existing board-level AFFILIATED_WITH edges for an organization.

    Returns list of dicts: [{name, role, startDate, compensation, compensationYear}]
    Only returns current members (endDate IS NULL).
    """
    cypher = """
    MATCH (p)-[r:AFFILIATED_WITH]->(o {name: $org_name})
    WHERE r.endDate IS NULL
    RETURN p.name AS name, r.role AS role, r.startDate AS startDate,
           r.compensation AS compensation, r.compensationYear AS compensationYear,
           r.source AS source
    """
    records, _ = execute_read(cypher, database=database, driver=driver, org_name=org_name)
    existing = []
    for rec in records:
        role = rec.get("role", "")
        if _is_board_role(role):
            existing.append({
                "name": rec["name"],
                "role": role,
                "startDate": rec.get("startDate"),
                "compensation": rec.get("compensation"),
                "compensationYear": rec.get("compensationYear"),
                "source": rec.get("source"),
            })
    return existing


def _compute_diff(existing, new_members):
    """Compare existing board against new snapshot.

    Returns dict with added, departed, role_changes, unchanged lists.
    """
    # Build lookup dicts by normalized name
    existing_by_name = {}
    for m in existing:
        key = m["name"].strip().lower()
        existing_by_name[key] = m

    new_by_name = {}
    for m in new_members:
        key = m["name"].strip().lower()
        new_by_name[key] = m

    added = []
    departed = []
    role_changes = []
    unchanged = []

    # Check new members against existing
    for key, new_m in new_by_name.items():
        if key in existing_by_name:
            old_m = existing_by_name[key]
            old_role = _normalize_role(old_m["role"])
            new_role = _normalize_role(new_m.get("role", ""))
            if old_role != new_role:
                role_changes.append({
                    "name": new_m["name"],
                    "old_role": old_m["role"],
                    "new_role": new_m.get("role", ""),
                })
            else:
                unchanged.append({"name": new_m["name"], "role": new_m.get("role", "")})
        else:
            added.append({"name": new_m["name"], "role": new_m.get("role", "")})

    # Check for departures (in existing but not in new)
    for key, old_m in existing_by_name.items():
        if key not in new_by_name:
            departed.append({"name": old_m["name"], "role": old_m["role"]})

    return {
        "added": added,
        "departed": departed,
        "role_changes": role_changes,
        "unchanged": unchanged,
    }


def board_snapshot_impl(organization, fiscal_year, members, source_url=None,
                        source=None, database=GRAPH_DATABASE, driver=None, **kwargs):
    """Load a board composition snapshot and detect changes.

    Args:
        organization: Organization name (must exist in graph)
        fiscal_year: Fiscal year string (e.g. "2024")
        members: List of member dicts: [{name, role, compensation?}]
        source_url: URL of the source document (e.g. 990 filing)
        source: Text description of the source
        database: Neo4j database (default: corcoran)
        driver: Shared Neo4j driver

    Returns:
        dict with diff results and operation counts
    """
    if not organization:
        return {"error": "Missing required parameter 'organization'"}
    if not fiscal_year:
        return {"error": "Missing required parameter 'fiscal_year'"}
    if not members or not isinstance(members, list):
        return {"error": "Missing or invalid 'members' list. Provide [{name, role, compensation?}, ...]"}

    # Validate organization exists
    check_records, _ = execute_read(
        "MATCH (o {name: $name}) RETURN labels(o) AS labels",
        database=database, driver=driver, name=organization
    )
    if not check_records:
        return {"error": f"Organization '{organization}' not found in graph. Create it first."}

    # Canonicalize source URL if provided
    canonical_source_url = canonicalize_url(source_url) if source_url else None

    # Build source description
    source_desc = source or f"990 filing FY{fiscal_year}"

    # Compute fiscal year dates (typical July FY: July 1 - June 30)
    try:
        fy = int(fiscal_year)
        start_date = f"{fy-1}-07"  # FY2024 starts July 2023
        end_date = f"{fy}-06"      # FY2024 ends June 2024
    except ValueError:
        start_date = fiscal_year
        end_date = None

    # Step 1: Get existing board composition
    existing = _get_existing_board(organization, database, driver)

    # Step 2: Compute diff
    diff = _compute_diff(existing, members)

    # Step 3: Execute graph operations
    nodes_created = 0
    nodes_updated = 0
    edges_created = 0
    edges_closed = 0
    properties_set = 0

    # 3a: Create/update Person nodes and AFFILIATED_WITH edges for all new members
    for member in members:
        name = member.get("name")
        role = member.get("role", "")
        compensation = member.get("compensation")

        if not name:
            continue

        # Create/update Person node
        cypher = """
        MERGE (p:Person {name: $name})
        ON CREATE SET p.addedDate = date(), p.source = $source
        RETURN p.name AS name, labels(p) AS labels
        """
        records, summary = execute_write(
            cypher, database=database, driver=driver,
            name=name, source=source_desc
        )
        if summary.counters.nodes_created > 0:
            nodes_created += 1
        else:
            nodes_updated += 1

        # Create AFFILIATED_WITH edge
        # Use MERGE on (person, org, role) to avoid duplicates for same role
        set_parts = ["r.startDate = $start_date", "r.source = $source"]
        params = {
            "person_name": name,
            "org_name": organization,
            "role": role,
            "start_date": start_date,
            "source": source_desc,
        }

        if canonical_source_url:
            set_parts.append("r.sourceUrl = $source_url")
            params["source_url"] = canonical_source_url

        if compensation is not None:
            set_parts.append("r.compensation = $compensation")
            set_parts.append("r.compensationYear = $comp_year")
            params["compensation"] = compensation
            params["comp_year"] = fiscal_year

        set_clause = ", ".join(set_parts)

        edge_cypher = f"""
        MATCH (p:Person {{name: $person_name}})
        MATCH (o {{name: $org_name}})
        MERGE (p)-[r:AFFILIATED_WITH {{role: $role}}]->(o)
        ON CREATE SET {set_clause}
        ON MATCH SET {set_clause}
        RETURN p.name AS person, type(r) AS rel, o.name AS org
        """
        records, summary = execute_write(edge_cypher, database=database, driver=driver, **params)
        edges_created += summary.counters.relationships_created
        properties_set += summary.counters.properties_set

    # 3b: Close departed members' edges (set endDate)
    for departed in diff["departed"]:
        close_cypher = """
        MATCH (p:Person {name: $name})-[r:AFFILIATED_WITH]->(o {name: $org_name})
        WHERE r.endDate IS NULL AND toLower(r.role) CONTAINS toLower($role_fragment)
        SET r.endDate = $end_date
        RETURN p.name AS person, r.role AS role
        """
        # Use first word of role as fragment for matching
        role_fragment = departed["role"].split()[0] if departed["role"] else ""
        records, summary = execute_write(
            close_cypher, database=database, driver=driver,
            name=departed["name"], org_name=organization,
            role_fragment=role_fragment, end_date=end_date or fiscal_year
        )
        if summary.counters.properties_set > 0:
            edges_closed += 1

    # 3c: For role changes, close old edge and create new one
    for change in diff["role_changes"]:
        # Close old role edge
        close_cypher = """
        MATCH (p:Person {name: $name})-[r:AFFILIATED_WITH {role: $old_role}]->(o {name: $org_name})
        WHERE r.endDate IS NULL
        SET r.endDate = $end_date
        RETURN p.name AS person
        """
        execute_write(
            close_cypher, database=database, driver=driver,
            name=change["name"], org_name=organization,
            old_role=change["old_role"], end_date=end_date or fiscal_year
        )
        edges_closed += 1
        # New role edge was already created in step 3a

    result = {
        "organization": organization,
        "fiscal_year": fiscal_year,
        "source": source_desc,
        "source_url": canonical_source_url,
        "diff": diff,
        "counts": {
            "members_loaded": len(members),
            "nodes_created": nodes_created,
            "nodes_updated": nodes_updated,
            "edges_created": edges_created,
            "edges_closed": edges_closed,
            "properties_set": properties_set,
        },
        "summary": (
            f"Loaded {len(members)} board members for {organization} FY{fiscal_year}. "
            f"Added: {len(diff['added'])}, Departed: {len(diff['departed'])}, "
            f"Role changes: {len(diff['role_changes'])}, Unchanged: {len(diff['unchanged'])}."
        ),
    }

    return result


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = board_snapshot_impl(**params)
    output(result)
