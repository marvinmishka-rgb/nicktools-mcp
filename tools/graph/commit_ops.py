"""Batch commit: execute multiple graph operations in a single tool call.
---
description: Execute a batch of node, rel, and wire_evidence operations sequentially
creates_nodes: [*]
creates_edges: [*]
databases: [corcoran, lifestream]
---

Accepts an array of operations and dispatches each to node_impl, rel_impl,
or wire_evidence_impl. Designed to close the verbosity gap between generic
tools and the old add_* wrappers -- a full entity commit (person + org +
employment + evidence) in one call instead of 4-5.

Semantics:
  - Sequential execution: order matters (create nodes before wiring rels)
  - Stop-on-error (default): if op N fails, ops 1..N-1 committed, N+1.. skipped
  - Continue-on-error (optional): attempt all ops, report per-op results
  - Each operation commits independently (MERGE idempotency makes this safe)
"""
import sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import GRAPH_DATABASE


# Valid operation types and their dispatchers
VALID_OPS = {"node", "rel", "wire_evidence"}


def _entity_exists(name, database, driver):
    """Check if an entity with the given name exists in the graph.

    Args:
        name: Entity name to look up
        database: Neo4j database name
        driver: Shared Neo4j driver

    Returns:
        True if entity exists, False otherwise
    """
    try:
        from lib.db import execute_read
        records, _ = execute_read(
            "MATCH (n {name: $name}) RETURN n.name LIMIT 1",
            database=database, driver=driver, name=name,
        )
        return bool(records)
    except Exception:
        # If the check itself fails (e.g. driver issue), don't block the batch.
        # The actual execution will surface the real error.
        return True


def _dispatch_op(op_dict, database, driver):
    """Dispatch a single operation dict to the appropriate _impl function.

    Args:
        op_dict: Dict with 'op' key and operation-specific params
        database: Neo4j database name
        driver: Shared Neo4j driver

    Returns:
        dict with operation result (may contain 'error' key on failure)
    """
    op_type = op_dict.get("op")
    if not op_type:
        # Accept 'type' as alias only if its value is a valid op name.
        # For rel ops, 'type' holds the relationship type (e.g. "EMPLOYED_BY"),
        # not the operation kind -- so we must not consume it as the op alias.
        candidate = op_dict.get("type")
        if candidate in VALID_OPS:
            op_type = candidate
    if not op_type:
        return {"error": "Missing 'op' key. Must be: node, rel, or wire_evidence"}
    if op_type not in VALID_OPS:
        return {"error": f"Invalid op '{op_type}'. Must be: {', '.join(sorted(VALID_OPS))}"}

    # Build params: strip 'op' always; only strip 'type' if it was used as the op alias
    params = {k: v for k, v in op_dict.items() if k != "op"}
    if params.get("type") == op_type:
        del params["type"]
    params["database"] = params.get("database", database)
    params["driver"] = driver

    try:
        if op_type == "node":
            from tools.graph.node_ops import node_impl
            # node_impl expects action, label, **kwargs (flat)
            action = params.pop("action", None)
            label = params.pop("label", None)
            if not action:
                return {"error": "node op missing 'action'. Must be: add, update, or get"}
            if not label:
                return {"error": "node op missing 'label'. E.g. 'Person', 'Organization'"}
            # Flatten 'props' or 'properties' dict if provided
            props = params.pop("props", None) or params.pop("properties", None)
            if isinstance(props, dict):
                params.update(props)
            return node_impl(action, label, **params)

        elif op_type == "rel":
            from tools.graph.rel_ops import rel_impl
            action = params.pop("action", None)
            rel_type = params.pop("rel_type", None) or params.pop("type", None)
            from_name = params.pop("from_name", None)
            to_name = params.pop("to_name", None)
            if not action:
                return {"error": "rel op missing 'action'. Must be: add, update, or remove"}
            if not rel_type:
                return {"error": "rel op missing 'type'. E.g. 'EMPLOYED_BY'"}
            if not from_name:
                return {"error": "rel op missing 'from_name'"}
            if not to_name:
                return {"error": "rel op missing 'to_name'"}
            return rel_impl(action, rel_type, from_name, to_name, **params)

        elif op_type == "wire_evidence":
            from tools.graph.wire_evidence import wire_evidence_impl
            entity = params.pop("entity", None)
            sources = params.pop("sources", None)
            if not entity:
                return {"error": "wire_evidence op missing 'entity'"}
            if not sources:
                return {"error": "wire_evidence op missing 'sources'"}
            return wire_evidence_impl(entity, sources, **params)

    except Exception as e:
        import traceback
        return {"error": f"{op_type} dispatch failed: {e}", "traceback": traceback.format_exc()}


def commit_impl(operations, continue_on_error=False, database=GRAPH_DATABASE, driver=None, **kwargs):
    """Execute a batch of graph operations sequentially.

    Args:
        operations: List of operation dicts. Each must have an 'op' key:
            - {op: "node", action: "add", label: "Person", name: "...", ...}
            - {op: "rel", action: "add", type: "EMPLOYED_BY", from_name: "...", to_name: "...", ...}
            - {op: "wire_evidence", entity: "...", sources: [...], ...}
        continue_on_error: If True, attempt all operations even if some fail.
            If False (default), stop at first error.
        database: Default Neo4j database for all operations (individual ops can override)
        driver: Shared Neo4j driver

    Returns:
        dict with:
        - results: List of per-operation results (same order as input)
        - summary: Aggregate counts (nodes_created, rels_created, evidence_wired, errors)
        - stopped_at: Index of first error (only if stop-on-error triggered)
    """
    if not operations:
        return {"error": "Missing 'operations' list. Provide a list of {op, ...} dicts."}
    if not isinstance(operations, list):
        return {"error": "'operations' must be a list of {op, ...} dicts."}

    # ---- Phase 1: Pre-validate all operations before executing any ----
    # This prevents partial batch failures where ops 1-4 execute but op 5
    # has a typo in the rel type, killing the remaining 5 ops.

    # Build set of entity names that will be created by node ops in this batch.
    # This prevents false-positive "entity not found" errors for rels that
    # reference nodes created earlier in the same batch.
    batch_node_names = set()
    for op_dict in operations:
        if isinstance(op_dict, dict) and (op_dict.get("op") or (op_dict.get("type") if op_dict.get("type") in VALID_OPS else None)) == "node":
            name = op_dict.get("name")
            # Also check inside 'props' or 'properties' dict if name is nested there
            if not name and isinstance(op_dict.get("props"), dict):
                name = op_dict["props"].get("name")
            if not name and isinstance(op_dict.get("properties"), dict):
                name = op_dict["properties"].get("name")
            if name:
                batch_node_names.add(name)

    validation_errors = []
    for i, op_dict in enumerate(operations):
        if not isinstance(op_dict, dict):
            validation_errors.append({"_index": i, "error": f"Operation {i} is not a dict: {type(op_dict).__name__}"})
            continue
        op_type = op_dict.get("op")
        if not op_type:
            candidate = op_dict.get("type")
            if candidate in VALID_OPS:
                op_type = candidate
        if not op_type:
            validation_errors.append({"_index": i, "error": "Missing 'op' key (or 'type'). Must be: node, rel, or wire_evidence"})
            continue
        if op_type not in VALID_OPS:
            validation_errors.append({"_index": i, "error": f"Invalid op '{op_type}'. Must be: {', '.join(sorted(VALID_OPS))}"})
            continue

        # Validate required fields per op type (without executing)
        if op_type == "node":
            if not op_dict.get("action"):
                validation_errors.append({"_index": i, "error": "node op missing 'action'. Must be: add, update, or get"})
            if not op_dict.get("label"):
                validation_errors.append({"_index": i, "error": "node op missing 'label'. E.g. 'Person', 'Organization'"})
            else:
                # Validate label exists in schema
                from lib.schema import validate_label
                ok, err = validate_label(op_dict["label"])
                if not ok:
                    validation_errors.append({"_index": i, "error": err})
        elif op_type == "rel":
            if not op_dict.get("action"):
                validation_errors.append({"_index": i, "error": "rel op missing 'action'. Must be: add, update, or remove"})
            # Accept 'rel_type' or 'type' for the relationship type
            resolved_rel_type = op_dict.get("rel_type") or op_dict.get("type")
            if not resolved_rel_type:
                validation_errors.append({"_index": i, "error": "rel op missing 'type' (or 'rel_type'). E.g. 'EMPLOYED_BY'"})
            else:
                from lib.schema import validate_rel_type
                # Use non-strict: unknown types auto-register with a warning
                ok, msg = validate_rel_type(resolved_rel_type, strict=False)
                if not ok:
                    validation_errors.append({"_index": i, "error": msg})
            if not op_dict.get("from_name"):
                validation_errors.append({"_index": i, "error": "rel op missing 'from_name'"})
            if not op_dict.get("to_name"):
                validation_errors.append({"_index": i, "error": "rel op missing 'to_name'"})

            # Entity existence pre-check: verify endpoints exist in graph
            # (skip names that will be created by preceding node ops in this batch)
            from_name = op_dict.get("from_name")
            to_name = op_dict.get("to_name")
            if from_name and from_name not in batch_node_names:
                if not _entity_exists(from_name, database, driver):
                    validation_errors.append({
                        "_index": i,
                        "error": f"Entity '{from_name}' not found in graph. "
                                 f"Create it first with a node op, or check the spelling."
                    })
            if to_name and to_name not in batch_node_names:
                if not _entity_exists(to_name, database, driver):
                    validation_errors.append({
                        "_index": i,
                        "error": f"Entity '{to_name}' not found in graph. "
                                 f"Create it first with a node op, or check the spelling."
                    })
        elif op_type == "wire_evidence":
            if not op_dict.get("entity"):
                validation_errors.append({"_index": i, "error": "wire_evidence op missing 'entity'"})
            if not op_dict.get("sources"):
                validation_errors.append({"_index": i, "error": "wire_evidence op missing 'sources'"})

    # If any validation errors, return them ALL without executing anything
    if validation_errors:
        return {
            "validation_errors": validation_errors,
            "summary": {
                "total_ops": len(operations),
                "completed": 0,
                "skipped": len(operations),
                "errors": len(validation_errors),
                "nodes_created": 0, "nodes_updated": 0,
                "rels_created": 0, "evidence_wired": 0,
            },
            "message": f"Pre-validation failed: {len(validation_errors)} error(s) found. No operations were executed. Fix the errors and retry."
        }

    # ---- Phase 2: Execute all validated operations ----
    results = []
    summary = {
        "total_ops": len(operations),
        "completed": 0,
        "skipped": 0,
        "errors": 0,
        "nodes_created": 0,
        "nodes_updated": 0,
        "rels_created": 0,
        "evidence_wired": 0,
    }
    stopped_at = None

    for i, op_dict in enumerate(operations):
        op_result = _dispatch_op(op_dict, database, driver)

        # Tag result with index and op type for clarity
        op_result["_index"] = i
        if isinstance(op_dict, dict):
            _op = op_dict.get("op")
            if not _op:
                candidate = op_dict.get("type")
                _op = candidate if candidate in VALID_OPS else "unknown"
            op_result["_op"] = _op
        else:
            op_result["_op"] = "unknown"

        has_error = "error" in op_result
        results.append(op_result)

        if has_error:
            summary["errors"] += 1
            if not continue_on_error:
                stopped_at = i
                # Mark remaining ops as skipped
                summary["skipped"] = len(operations) - i - 1
                break
        else:
            summary["completed"] += 1
            # Aggregate counters based on op type
            op_type = op_result.get("_op")
            if op_type == "node":
                if op_result.get("created"):
                    summary["nodes_created"] += 1
                elif op_result.get("updated") or op_result.get("found"):
                    summary["nodes_updated"] += 1
            elif op_type == "rel":
                summary["rels_created"] += op_result.get("relationships_created", 0)
            elif op_type == "wire_evidence":
                summary["evidence_wired"] += op_result.get("edges_wired", 0)

    response = {
        "results": results,
        "summary": summary,
    }
    if stopped_at is not None:
        response["stopped_at"] = stopped_at

    return response


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = commit_impl(**params)
    output(result)
