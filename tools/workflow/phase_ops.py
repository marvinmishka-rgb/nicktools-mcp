"""Phase tracking: create, update, transition, and query project phases.
---
description: Project phase lifecycle management with dependency tracking
creates_nodes: [Phase]
creates_edges: [DEPENDS_ON, DOCUMENTED_BY, WORKED_IN, HAS_SUBPHASE]
databases: [lifestream]
---

Phases model discrete units of work within a project. They track status,
dependencies, linked entries, and session history -- enabling context recovery
across sessions without reading plan files.

Actions:
  create     -- Create a new Phase node
  update     -- Update phase properties (title, description, notes)
  transition -- Change status with auto-timestamping
  get        -- Return phase with all relationships
  list       -- List all phases for a project (with status summary)
  link       -- Wire relationships (DEPENDS_ON, DOCUMENTED_BY, WORKED_IN, HAS_SUBPHASE)
"""
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.db import get_neo4j_driver, ENTRY_DATABASE
from lib.io import setup_output, load_params, output


# Valid status values and allowed transitions
VALID_STATUSES = {"planned", "in_progress", "complete", "blocked", "deferred"}
# Accept hyphenated forms as aliases (e.g. "in-progress" -> "in_progress")
_STATUS_ALIASES = {"in-progress": "in_progress", "in_progress": "in_progress"}

def _normalize_status(s):
    """Normalize status to canonical underscore form. Accepts hyphens or underscores."""
    if s is None:
        return s
    return _STATUS_ALIASES.get(s, s)

STATUS_TRANSITIONS = {
    "planned":     {"in_progress", "deferred", "blocked"},
    "in_progress": {"complete", "blocked", "deferred"},
    "blocked":     {"in_progress", "deferred", "planned"},
    "deferred":    {"planned", "in_progress"},
    "complete":    {"in_progress"},  # allow reopening
}

# Valid link types and their Cypher patterns
LINK_TYPES = {
    "depends_on":    {"rel": "DEPENDS_ON",    "direction": "out", "target_label": "Phase"},
    "documented_by": {"rel": "DOCUMENTED_BY", "direction": "in",  "target_label": "StreamEntry"},
    "worked_in":     {"rel": "WORKED_IN",     "direction": "out", "target_label": "CoworkSession"},
    "has_subphase":  {"rel": "HAS_SUBPHASE",  "direction": "out", "target_label": "Phase"},
}


def _build_phase_id(project, phase_number):
    """Build a canonical phaseId from project and phase_number."""
    return f"{project}/{phase_number}"


def _create(project, phase_number, title, description="", status="planned",
            notes="", plan_file="", parent_phase=None, depends_on=None, driver=None):
    """Create a new Phase node.

    Args:
        project: Project identifier (e.g. "nicktools-1.0")
        phase_number: Phase number/code (e.g. "4f", "1", "7a")
        title: Human-readable title
        description: Scope/goals description
        status: Initial status (default: planned)
        notes: Freeform notes
        plan_file: Path to the plan document (stored on the node for traceability)
        parent_phase: Phase number of parent (e.g. "4" for sub-phase "4f")
        depends_on: List of phase numbers this phase depends on

    Returns:
        dict with phaseId, created, status, warnings
    """
    status = _normalize_status(status)
    if status not in VALID_STATUSES:
        return {"error": f"Invalid status '{status}'. Valid: {sorted(VALID_STATUSES)}"}

    phase_id = _build_phase_id(project, phase_number)
    depends_on = depends_on or []
    result = {"action": "create", "phase_id": phase_id, "warnings": []}

    _driver = driver or get_neo4j_driver()
    try:
        with _driver.session(database=ENTRY_DATABASE) as session:
            # Create the Phase node
            rec = session.run("""
                MERGE (p:Phase {phaseId: $phaseId})
                ON CREATE SET
                    p.project = $project,
                    p.phaseNumber = $phaseNumber,
                    p.title = $title,
                    p.description = $description,
                    p.status = $status,
                    p.notes = $notes,
                    p.planFile = $planFile,
                    p.createdAt = datetime(),
                    p.sessionCount = 0
                ON MATCH SET
                    p.title = $title,
                    p.description = $description,
                    p.notes = $notes
                RETURN p, p.status AS status,
                       CASE WHEN p.createdAt = datetime() THEN true ELSE false END AS created
            """, {
                "phaseId": phase_id,
                "project": project,
                "phaseNumber": phase_number,
                "title": title,
                "description": description,
                "status": status,
                "notes": notes,
                "planFile": plan_file,
            }).single()

            if rec:
                result["created"] = True  # MERGE semantics — we report success
                result["status"] = rec["status"]
            else:
                result["error"] = "MERGE returned no record"
                return result

            # Wire parent phase if specified
            if parent_phase:
                parent_id = _build_phase_id(project, parent_phase)
                parent_rec = session.run("""
                    MATCH (parent:Phase {phaseId: $parentId})
                    MATCH (child:Phase {phaseId: $childId})
                    MERGE (parent)-[r:HAS_SUBPHASE]->(child)
                    ON CREATE SET r.createdAt = datetime()
                    RETURN parent.phaseNumber AS parentNumber
                """, {"parentId": parent_id, "childId": phase_id}).single()

                if parent_rec:
                    result["parent_phase"] = parent_rec["parentNumber"]
                else:
                    result["warnings"].append(
                        f"Parent phase '{parent_phase}' not found in project '{project}'"
                    )

            # Wire dependencies
            deps_wired = 0
            for dep_number in depends_on:
                dep_id = _build_phase_id(project, dep_number)
                dep_rec = session.run("""
                    MATCH (phase:Phase {phaseId: $phaseId})
                    MATCH (dep:Phase {phaseId: $depId})
                    MERGE (phase)-[r:DEPENDS_ON]->(dep)
                    ON CREATE SET r.createdAt = datetime()
                    RETURN dep.phaseNumber AS depNumber
                """, {"phaseId": phase_id, "depId": dep_id}).single()

                if dep_rec:
                    deps_wired += 1
                else:
                    result["warnings"].append(
                        f"Dependency phase '{dep_number}' not found in project '{project}'"
                    )

            if depends_on:
                result["dependencies_wired"] = deps_wired

    except Exception as e:
        result["error"] = str(e)
        import traceback
        result["traceback"] = traceback.format_exc()
    finally:
        if not driver:
            _driver.close()

    return result


def _update(project, phase_number, driver=None, **kwargs):
    """Update Phase properties (not status -- use transition for that).

    Args:
        project: Project identifier
        phase_number: Phase number/code
        **kwargs: Properties to update (title, description, notes)

    Returns:
        dict with phase_id, updated, properties_set
    """
    phase_id = _build_phase_id(project, phase_number)
    result = {"action": "update", "phase_id": phase_id, "warnings": []}

    # Filter to updateable properties
    updateable = {"title", "description", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in updateable and v is not None}

    if not updates:
        return {"error": f"No updateable properties provided. Updateable: {sorted(updateable)}. "
                         "Use 'transition' action to change status."}

    _driver = driver or get_neo4j_driver()
    try:
        with _driver.session(database=ENTRY_DATABASE) as session:
            # Build dynamic SET clause
            set_parts = [f"p.{k} = ${k}" for k in updates]
            set_clause = ", ".join(set_parts)

            rec = session.run(f"""
                MATCH (p:Phase {{phaseId: $phaseId}})
                SET {set_clause}, p.updatedAt = datetime()
                RETURN p.title AS title, p.status AS status
            """, {"phaseId": phase_id, **updates}).single()

            if rec:
                result["updated"] = True
                result["title"] = rec["title"]
                result["status"] = rec["status"]
                result["properties_set"] = list(updates.keys())
            else:
                result["error"] = f"Phase '{phase_number}' not found in project '{project}'"

    except Exception as e:
        result["error"] = str(e)
    finally:
        if not driver:
            _driver.close()

    return result


def _transition(project, phase_number, new_status, reason="", driver=None):
    """Transition a phase to a new status with auto-timestamping.

    Args:
        project: Project identifier
        phase_number: Phase number/code
        new_status: Target status
        reason: Reason for blocked/deferred transitions

    Returns:
        dict with phase_id, old_status, new_status, timestamp
    """
    new_status = _normalize_status(new_status)
    if new_status not in VALID_STATUSES:
        return {"error": f"Invalid status '{new_status}'. Valid: {sorted(VALID_STATUSES)}"}

    phase_id = _build_phase_id(project, phase_number)
    result = {"action": "transition", "phase_id": phase_id, "warnings": []}

    _driver = driver or get_neo4j_driver()
    try:
        with _driver.session(database=ENTRY_DATABASE) as session:
            # Get current status
            rec = session.run("""
                MATCH (p:Phase {phaseId: $phaseId})
                RETURN p.status AS currentStatus
            """, {"phaseId": phase_id}).single()

            if not rec:
                return {"error": f"Phase '{phase_number}' not found in project '{project}'"}

            raw_status = rec["currentStatus"]
            old_status = _normalize_status(raw_status)

            # Auto-heal: if stored status was hyphenated, fix it
            if raw_status != old_status:
                session.run("MATCH (p:Phase {phaseId: $phaseId}) SET p.status = $status",
                            {"phaseId": phase_id, "status": old_status})
                result["warnings"].append(f"Auto-healed status '{raw_status}' -> '{old_status}'")

            # Validate transition
            allowed = STATUS_TRANSITIONS.get(old_status, set())
            if new_status not in allowed:
                return {"error": f"Cannot transition from '{old_status}' to '{new_status}'. "
                                 f"Allowed transitions from '{old_status}': {sorted(allowed)}"}

            # Build timestamp fields based on target status
            timestamp_sets = ["p.status = $newStatus"]
            params = {"phaseId": phase_id, "newStatus": new_status}

            if new_status == "in_progress":
                # Only set startedAt on first transition to in_progress
                timestamp_sets.append(
                    "p.startedAt = CASE WHEN p.startedAt IS NULL THEN datetime() ELSE p.startedAt END"
                )
                # Clear blocked fields if resuming from blocked
                timestamp_sets.append("p.blockedAt = null")
                timestamp_sets.append("p.blockedReason = null")

            elif new_status == "complete":
                timestamp_sets.append("p.completedAt = datetime()")

            elif new_status == "blocked":
                timestamp_sets.append("p.blockedAt = datetime()")
                if reason:
                    timestamp_sets.append("p.blockedReason = $reason")
                    params["reason"] = reason

            elif new_status == "deferred":
                timestamp_sets.append("p.deferredAt = datetime()")
                if reason:
                    timestamp_sets.append("p.deferredReason = $reason")
                    params["reason"] = reason

            set_clause = ", ".join(timestamp_sets)

            rec = session.run(f"""
                MATCH (p:Phase {{phaseId: $phaseId}})
                SET {set_clause}
                RETURN p.status AS status,
                       toString(p.startedAt) AS startedAt,
                       toString(p.completedAt) AS completedAt
            """, params).single()

            result["old_status"] = old_status
            result["new_status"] = rec["status"]
            result["started_at"] = rec["startedAt"]
            result["completed_at"] = rec["completedAt"]

            # Auto-link to current CoworkSession when transitioning to in_progress
            if new_status == "in_progress":
                try:
                    from lib.session_detect import get_cached_session
                    cached = get_cached_session()
                    if cached and cached.get("sessionId"):
                        link_rec = session.run("""
                            MATCH (p:Phase {phaseId: $phaseId})
                            MATCH (cs:CoworkSession {sessionId: $sessionId})
                            MERGE (p)-[r:WORKED_IN]->(cs)
                            ON CREATE SET r.createdAt = datetime(),
                                          p.sessionCount = coalesce(p.sessionCount, 0) + 1
                            RETURN cs.title AS sessionTitle
                        """, {"phaseId": phase_id, "sessionId": cached["sessionId"]}).single()

                        if link_rec:
                            result["session_linked"] = link_rec["sessionTitle"]
                except Exception:
                    pass  # Non-critical

    except Exception as e:
        result["error"] = str(e)
        import traceback
        result["traceback"] = traceback.format_exc()
    finally:
        if not driver:
            _driver.close()

    return result


def _get(project, phase_number, driver=None):
    """Get a phase with all its relationships.

    Args:
        project: Project identifier
        phase_number: Phase number/code

    Returns:
        dict with phase properties and related entities
    """
    phase_id = _build_phase_id(project, phase_number)
    result = {"action": "get", "phase_id": phase_id}

    _driver = driver or get_neo4j_driver()
    try:
        with _driver.session(database=ENTRY_DATABASE) as session:
            rec = session.run("""
                MATCH (p:Phase {phaseId: $phaseId})
                OPTIONAL MATCH (p)-[:DEPENDS_ON]->(dep:Phase)
                OPTIONAL MATCH (p)<-[:DEPENDS_ON]-(blocked_by:Phase)
                OPTIONAL MATCH (p)-[:HAS_SUBPHASE]->(sub:Phase)
                OPTIONAL MATCH (p)<-[:HAS_SUBPHASE]-(parent:Phase)
                OPTIONAL MATCH (p)<-[doc_r:DOCUMENTED_BY]-(entry:StreamEntry)
                OPTIONAL MATCH (p)-[:WORKED_IN]->(cs:CoworkSession)
                RETURN p,
                    collect(DISTINCT {id: dep.phaseId, number: dep.phaseNumber,
                            title: dep.title, status: dep.status}) AS dependencies,
                    collect(DISTINCT {id: blocked_by.phaseId, number: blocked_by.phaseNumber,
                            title: blocked_by.title, status: blocked_by.status}) AS blocks,
                    collect(DISTINCT {id: sub.phaseId, number: sub.phaseNumber,
                            title: sub.title, status: sub.status}) AS subphases,
                    CASE WHEN parent IS NOT NULL
                        THEN {id: parent.phaseId, number: parent.phaseNumber, title: parent.title}
                        ELSE null END AS parent,
                    collect(DISTINCT {id: entry.id, title: entry.title,
                            type: entry.type}) AS entries,
                    collect(DISTINCT {title: cs.title,
                            processName: cs.processName}) AS sessions
            """, {"phaseId": phase_id}).single()

            if not rec:
                return {"error": f"Phase '{phase_number}' not found in project '{project}'"}

            node = dict(rec["p"])
            # Convert Neo4j datetime objects to strings
            for key in ["createdAt", "startedAt", "completedAt", "blockedAt",
                        "deferredAt", "updatedAt"]:
                if key in node and node[key] is not None:
                    node[key] = str(node[key])

            result["phase"] = node
            result["dependencies"] = [d for d in rec["dependencies"] if d.get("id")]
            result["blocks"] = [b for b in rec["blocks"] if b.get("id")]
            result["subphases"] = [s for s in rec["subphases"] if s.get("id")]
            result["parent"] = rec["parent"]
            result["entries"] = [e for e in rec["entries"] if e.get("id")]
            result["sessions"] = [s for s in rec["sessions"] if s.get("title")]

    except Exception as e:
        result["error"] = str(e)
        import traceback
        result["traceback"] = traceback.format_exc()
    finally:
        if not driver:
            _driver.close()

    return result


def _list(project, status=None, driver=None):
    """List all phases for a project with status summary.

    Args:
        project: Project identifier
        status: Optional filter by status

    Returns:
        dict with phases list and summary counts
    """
    result = {"action": "list", "project": project}
    if status:
        status = _normalize_status(status)

    _driver = driver or get_neo4j_driver()
    try:
        with _driver.session(database=ENTRY_DATABASE) as session:
            # Build optional status filter
            status_clause = "WHERE p.status = $status" if status else ""
            params = {"project": project}
            if status:
                params["status"] = status

            records = session.run(f"""
                MATCH (p:Phase {{project: $project}})
                {status_clause}
                OPTIONAL MATCH (p)-[:DEPENDS_ON]->(dep:Phase)
                OPTIONAL MATCH (p)-[:HAS_SUBPHASE]->(sub:Phase)
                OPTIONAL MATCH (p)<-[:DOCUMENTED_BY]-(entry:StreamEntry)
                WITH p,
                     collect(DISTINCT dep.phaseNumber) AS deps,
                     collect(DISTINCT sub.phaseNumber) AS subs,
                     count(DISTINCT entry) AS entryCount
                ORDER BY p.phaseNumber
                RETURN p.phaseId AS phaseId,
                       p.phaseNumber AS phaseNumber,
                       p.title AS title,
                       p.status AS status,
                       p.description AS description,
                       toString(p.startedAt) AS startedAt,
                       toString(p.completedAt) AS completedAt,
                       p.sessionCount AS sessionCount,
                       deps, subs, entryCount
            """, params).data()

            phases = []
            for rec in records:
                phase = dict(rec)
                # Clean up null collections
                phase["deps"] = [d for d in phase.get("deps", []) if d]
                phase["subs"] = [s for s in phase.get("subs", []) if s]
                phases.append(phase)

            result["phases"] = phases
            result["count"] = len(phases)

            # Summary by status
            summary = {}
            for p in phases:
                s = p.get("status", "unknown")
                summary[s] = summary.get(s, 0) + 1
            result["summary"] = summary

    except Exception as e:
        result["error"] = str(e)
    finally:
        if not driver:
            _driver.close()

    return result


def _link(project, phase_number, link_type, target, driver=None):
    """Wire a relationship from/to a phase.

    Args:
        project: Project identifier
        phase_number: Phase number/code
        link_type: One of: depends_on, documented_by, worked_in, has_subphase
        target: Target identifier:
            - depends_on: phase number (e.g. "4e")
            - documented_by: entry ID (e.g. "ls-20260303-012")
            - worked_in: session processName (e.g. "compassionate-ecstatic-newton")
            - has_subphase: phase number (e.g. "4f")

    Returns:
        dict with phase_id, link_type, target, linked
    """
    if link_type not in LINK_TYPES:
        return {"error": f"Invalid link_type '{link_type}'. "
                         f"Valid: {sorted(LINK_TYPES.keys())}"}

    phase_id = _build_phase_id(project, phase_number)
    link_info = LINK_TYPES[link_type]
    result = {"action": "link", "phase_id": phase_id, "link_type": link_type,
              "target": target, "warnings": []}

    _driver = driver or get_neo4j_driver()
    try:
        with _driver.session(database=ENTRY_DATABASE) as session:
            rel_type = link_info["rel"]
            target_label = link_info["target_label"]
            direction = link_info["direction"]

            # Build match clause for target
            if link_type == "depends_on":
                target_id = _build_phase_id(project, target)
                target_match = f"MATCH (t:{target_label} {{phaseId: $targetId}})"
                target_params = {"targetId": target_id}
            elif link_type == "has_subphase":
                target_id = _build_phase_id(project, target)
                target_match = f"MATCH (t:{target_label} {{phaseId: $targetId}})"
                target_params = {"targetId": target_id}
            elif link_type == "documented_by":
                target_match = f"MATCH (t:{target_label} {{id: $targetId}})"
                target_params = {"targetId": target}
            elif link_type == "worked_in":
                # Match by processName (partial match) or sessionId
                target_match = (
                    f"MATCH (t:{target_label}) "
                    f"WHERE t.processName CONTAINS $targetId OR t.sessionId = $targetId"
                )
                target_params = {"targetId": target}

            # Build relationship creation
            if direction == "out":
                rel_pattern = f"(p)-[r:{rel_type}]->(t)"
            else:
                rel_pattern = f"(t)-[r:{rel_type}]->(p)"

            rec = session.run(f"""
                MATCH (p:Phase {{phaseId: $phaseId}})
                {target_match}
                MERGE {rel_pattern}
                ON CREATE SET r.createdAt = datetime()
                RETURN t, labels(t) AS labels
            """, {"phaseId": phase_id, **target_params}).single()

            if rec:
                result["linked"] = True
                target_node = dict(rec["t"])
                result["target_name"] = (target_node.get("title")
                                        or target_node.get("name")
                                        or target_node.get("processName")
                                        or target_node.get("id")
                                        or str(target))

                # Increment sessionCount for worked_in links
                if link_type == "worked_in":
                    session.run("""
                        MATCH (p:Phase {phaseId: $phaseId})
                        SET p.sessionCount = coalesce(p.sessionCount, 0) + 1
                    """, {"phaseId": phase_id})
            else:
                result["linked"] = False
                result["warnings"].append(
                    f"Target not found: {link_type} -> '{target}'"
                )

    except Exception as e:
        result["error"] = str(e)
        import traceback
        result["traceback"] = traceback.format_exc()
    finally:
        if not driver:
            _driver.close()

    return result


# ============================================================
# Main dispatch
# ============================================================

def phase_impl(action, project=None, phase_number=None, driver=None, **kwargs):
    """Phase lifecycle management.

    Args:
        action: create, update, transition, get, list, link
        project: Project identifier (required for all actions)
        phase_number: Phase number/code (required for all except list)
        driver: Optional shared Neo4j driver
        **kwargs: Action-specific parameters

    Returns:
        dict with action results
    """
    if not action:
        return {"error": "Missing 'action'. Valid: create, update, transition, get, list, link"}

    if not project:
        return {"error": "Missing 'project'. Provide a project identifier (e.g. 'nicktools-1.0')"}

    if action == "list":
        return _list(project, status=kwargs.get("status"), driver=driver)

    if not phase_number:
        return {"error": f"Missing 'phase_number' for action '{action}'. "
                         "Provide a phase number (e.g. '4f', '1', '7a')"}

    if action == "create":
        return _create(
            project=project,
            phase_number=phase_number,
            title=kwargs.get("title", f"Phase {phase_number}"),
            description=kwargs.get("description", ""),
            status=kwargs.get("status", "planned"),
            notes=kwargs.get("notes", ""),
            plan_file=kwargs.get("plan_file", ""),
            parent_phase=kwargs.get("parent_phase"),
            depends_on=kwargs.get("depends_on"),
            driver=driver,
        )
    elif action == "update":
        return _update(project=project, phase_number=phase_number,
                       driver=driver, **kwargs)
    elif action == "transition":
        new_status = kwargs.get("new_status") or kwargs.get("status")
        if not new_status:
            return {"error": "Missing 'new_status' for transition action. "
                             f"Valid statuses: {sorted(VALID_STATUSES)}"}
        return _transition(project=project, phase_number=phase_number,
                          new_status=new_status, reason=kwargs.get("reason", ""),
                          driver=driver)
    elif action == "get":
        return _get(project=project, phase_number=phase_number, driver=driver)
    elif action == "link":
        link_type = kwargs.get("link_type")
        target = kwargs.get("target")
        if not link_type or not target:
            return {"error": "Missing 'link_type' and/or 'target' for link action. "
                             f"Valid link_types: {sorted(LINK_TYPES.keys())}"}
        return _link(project=project, phase_number=phase_number,
                    link_type=link_type, target=target, driver=driver)
    else:
        return {"error": f"Unknown action '{action}'. "
                         "Valid: create, update, transition, get, list, link"}


# Subprocess entry point
if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = phase_impl(**params)
    output(result)
