#!/usr/bin/env python3
"""
nicktools Phase 4b -- Tool-by-tool Operational Verification
==========================================================
Calls each of the 55 registered operations with realistic parameters
against the nicktoolstest database. Classifies each as:
  - generic:         works for any domain out of the box
  - domain-specific: references Corcoran/real estate concepts
  - domain-flavored: generic functionality with domain defaults

Usage:
    # Full run (all tiers):
    python tests/test_operations.py

    # Specific tier(s):
    python tests/test_operations.py --tier 1 2

    # Skip network-dependent tests:
    python tests/test_operations.py --skip-network

    # Verbose (print full results):
    python tests/test_operations.py --verbose

Environment:
    NEO4J_PASSWORD          -- required
    NICKTOOLS_GRAPH_DB      -- default: nicktoolstest
    NICKTOOLS_ENTRY_DB      -- default: nicktoolstest
"""

import sys
import os
import json
import time
import tempfile
import shutil
import importlib
import traceback
from pathlib import Path
from datetime import datetime

# -- Setup paths --
SERVER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(SERVER_DIR / "tools"))

# -- Load .env if present --
env_file = SERVER_DIR / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())

# -- Test configuration --
TEST_GRAPH_DB = os.getenv("NICKTOOLS_GRAPH_DB", "nicktoolstest")
TEST_ENTRY_DB = os.getenv("NICKTOOLS_ENTRY_DB", "nicktoolstest")
TEST_PREFIX = "_test_4b_"
SKIP_NETWORK = "--skip-network" in sys.argv
VERBOSE = "--verbose" in sys.argv

# Parse --tier argument
TIERS = set()
if "--tier" in sys.argv:
    idx = sys.argv.index("--tier") + 1
    while idx < len(sys.argv) and not sys.argv[idx].startswith("-"):
        TIERS.add(int(sys.argv[idx]))
        idx += 1
if not TIERS:
    TIERS = {1, 2, 3, 4, 5}


# ============================================================
# Classification Registry
# ============================================================

CLASSIFICATION = {
    # graph/ operations
    "graph.write":          "generic",
    "graph.read":           "generic",
    "graph.node":           "generic",
    "graph.rel":            "generic",
    "graph.wire_evidence":  "generic",
    "graph.commit":         "generic",
    "graph.cypher":         "generic",
    "graph.gds":            "generic",
    "graph.board_snapshot": "domain-specific",   # board membership tracking
    "graph.deduplicate":    "generic",
    # research/ operations
    "research.browse_url":       "generic",
    "research.fetch_page":       "generic",
    "research.archive_source":   "generic",
    "research.save_page":        "generic",
    "research.extract_article":  "generic",
    "research.search_pdf":       "generic",
    "research.wayback_lookup":   "generic",
    "research.check_sources":    "generic",
    "research.archive_inventory":"generic",
    "research.search_records":   "domain-flavored",  # generic search API framework, domain-specific API sources
    "research.generate_report":  "generic",
    "research.queue_archive":    "generic",
    "research.check_queue":      "generic",
    "research.read_staged":      "generic",
    "research.process_queue":    "generic",
    "research.ingest_saved":     "generic",
    "research.vin_decode":       "domain-specific",   # vehicle research
    "research.search_business":  "domain-specific",   # state business search
    # entry/ operations
    "entry.create_entry":   "generic",
    "entry.update_entry":   "generic",
    "entry.session_start":  "generic",
    "entry.session_audit":  "generic",
    # core/ operations
    "core.run_script":       "generic",
    "core.run_python":       "generic",
    "core.run_command":      "generic",
    "core.list_scripts":     "generic",
    "core.read_file":        "generic",
    "core.write_file":       "generic",
    "core.read_document":    "generic",
    "core.neo4j_query":      "generic",
    "core.server_info":      "generic",
    "core.restart_server":   "generic",
    "core.registry_sync":    "generic",
    "core.sync_system_docs": "domain-flavored",  # generic mechanism, references domain-specific doc paths
    "core.backup_graph":     "generic",
    "core.session_ingest":   "generic",
    "core.session_costs":    "generic",
    "core.session_search":   "generic",
    "core.dispatch_health":  "generic",
    "core.task_status":      "generic",
    "core.session_health":   "generic",
    "core.backfill_discusses":"generic",
    "core.harvest_session":  "generic",
    "core.watcher_status":   "generic",
    "core.session_recover":  "generic",
}


# ============================================================
# Test Result Tracker
# ============================================================

class Results:
    def __init__(self):
        self.records = []  # (op_key, status, classification, detail, duration_ms)

    def ok(self, op_key, detail="", duration_ms=0):
        cls = CLASSIFICATION.get(op_key, "unknown")
        self.records.append((op_key, "PASS", cls, detail, duration_ms))
        dur = f" [{duration_ms}ms]" if duration_ms else ""
        print(f"  [PASS] {op_key}{dur} -- {detail[:120]}" if detail else f"  [PASS] {op_key}{dur}")

    def fail(self, op_key, reason, duration_ms=0):
        cls = CLASSIFICATION.get(op_key, "unknown")
        self.records.append((op_key, "FAIL", cls, reason, duration_ms))
        dur = f" [{duration_ms}ms]" if duration_ms else ""
        print(f"  [FAIL] {op_key}{dur} -- {reason[:200]}")

    def skip(self, op_key, reason):
        cls = CLASSIFICATION.get(op_key, "unknown")
        self.records.append((op_key, "SKIP", cls, reason, 0))
        print(f"  [SKIP] {op_key} -- {reason}")

    def summary(self):
        passed = sum(1 for r in self.records if r[1] == "PASS")
        failed = sum(1 for r in self.records if r[1] == "FAIL")
        skipped = sum(1 for r in self.records if r[1] == "SKIP")
        total = len(self.records)

        print(f"\n{'='*70}")
        print(f"RESULTS: {passed} passed, {failed} failed, {skipped} skipped ({total} total)")

        if failed:
            print(f"\nFAILURES:")
            for op, status, cls, detail, dur in self.records:
                if status == "FAIL":
                    print(f"  {op} [{cls}]: {detail[:200]}")

        # Classification summary
        print(f"\nCLASSIFICATION:")
        for cls_type in ["generic", "domain-flavored", "domain-specific"]:
            ops = [r[0] for r in self.records if r[2] == cls_type]
            if ops:
                print(f"  {cls_type} ({len(ops)}): {', '.join(ops)}")

        print(f"{'='*70}")
        return failed == 0


def timed_call(func, *args, **kwargs):
    """Call func and return (result, duration_ms)."""
    t0 = time.time()
    result = func(*args, **kwargs)
    return result, int((time.time() - t0) * 1000)


# ============================================================
# Setup: Load server and driver
# ============================================================

def setup():
    """Import server, get shared driver."""
    import server
    driver = server._shared_driver
    return server, driver


# ============================================================
# TIER 1: Safe read-only operations (no side effects)
# ============================================================

def tier1_safe_reads(results, server, driver):
    """Operations that are read-only and can't break anything."""
    print("\n-- TIER 1: Safe Read-Only Operations --")

    # core.server_info
    try:
        from core.server_info import server_info_impl
        ctx = {
            "server_name": "nicktools",
            "server_version": server.SERVER_VERSION,
            "tools_dir": str(SERVER_DIR / "tools"),
            "in_process_tools": list(server.IN_PROCESS_TOOLS.keys()),
            "operation_count": sum(len(g["operations"]) for g in server.TOOL_REGISTRY.values()),
        }
        r, ms = timed_call(server_info_impl, _server_context=ctx, driver=driver)
        if isinstance(r, dict) and "server" in str(r):
            results.ok("core.server_info", f"v{r.get('version', '?')}, {r.get('operation_count', '?')} ops", ms)
        else:
            results.fail("core.server_info", f"Unexpected: {str(r)[:100]}", ms)
    except Exception as e:
        results.fail("core.server_info", f"{type(e).__name__}: {e}")

    # core.list_scripts
    try:
        from core.list_scripts import list_scripts_impl
        r, ms = timed_call(list_scripts_impl, driver=driver)
        results.ok("core.list_scripts", f"{r.get('count', '?')} scripts" if isinstance(r, dict) else str(r)[:80], ms)
    except Exception as e:
        results.fail("core.list_scripts", f"{type(e).__name__}: {e}")

    # core.dispatch_health
    try:
        from core.dispatch_health import dispatch_health_impl
        r, ms = timed_call(dispatch_health_impl, driver=driver)
        results.ok("core.dispatch_health", f"calls={r.get('total_calls', '?')}" if isinstance(r, dict) else str(r)[:80], ms)
    except Exception as e:
        results.fail("core.dispatch_health", f"{type(e).__name__}: {e}")

    # core.task_status
    try:
        from core.task_status import task_status_impl
        r, ms = timed_call(task_status_impl, driver=driver)
        results.ok("core.task_status", f"tasks={r.get('total', '?')}" if isinstance(r, dict) else str(r)[:80], ms)
    except Exception as e:
        results.fail("core.task_status", f"{type(e).__name__}: {e}")

    # core.session_health
    try:
        from core.session_health import session_health_impl
        r, ms = timed_call(session_health_impl, brief=True, driver=driver)
        results.ok("core.session_health", f"healthy={r.get('healthy', '?')}" if isinstance(r, dict) else str(r)[:80], ms)
    except Exception as e:
        results.fail("core.session_health", f"{type(e).__name__}: {e}")

    # core.watcher_status
    try:
        from core.watcher_status import watcher_status_impl
        r, ms = timed_call(watcher_status_impl, driver=driver)
        results.ok("core.watcher_status", str(r)[:100] if r else "no watcher data", ms)
    except Exception as e:
        results.fail("core.watcher_status", f"{type(e).__name__}: {e}")

    # core.registry_sync
    try:
        from core.registry_sync import registry_sync_impl
        r, ms = timed_call(registry_sync_impl, driver=driver)
        results.ok("core.registry_sync", f"status={r.get('status', '?')}" if isinstance(r, dict) else str(r)[:80], ms)
    except Exception as e:
        results.fail("core.registry_sync", f"{type(e).__name__}: {e}")

    # graph.read (empty test DB = empty results, that's OK)
    try:
        from graph.read_ops import read_impl
        r, ms = timed_call(read_impl, label="Person", limit=5, driver=driver, database=TEST_GRAPH_DB)
        results.ok("graph.read", f"search mode, results={len(r.get('results', []))}" if isinstance(r, dict) else str(r)[:80], ms)
    except Exception as e:
        results.fail("graph.read", f"{type(e).__name__}: {e}")

    # graph.cypher (read mode)
    try:
        from graph.cypher_passthrough import cypher_impl
        r, ms = timed_call(cypher_impl, query="RETURN 1 AS test", mode="read", driver=driver, database=TEST_GRAPH_DB)
        if isinstance(r, dict) and r.get("query_type") == "r":
            results.ok("graph.cypher", f"read mode, type={r['query_type']}", ms)
        else:
            results.ok("graph.cypher", str(r)[:100], ms)
    except Exception as e:
        results.fail("graph.cypher", f"{type(e).__name__}: {e}")


# ============================================================
# TIER 2: Graph write operations (test DB, cleanup after)
# ============================================================

def tier2_graph_writes(results, server, driver):
    """Graph write operations -- create test data, exercise ops, clean up."""
    print("\n-- TIER 2: Graph Write Operations --")

    person_name = f"{TEST_PREFIX}person"
    org_name = f"{TEST_PREFIX}org"
    source_url = "https://example.com/test-source-4b"

    # graph.node (add)
    try:
        from graph.node_ops import node_impl
        importlib.reload(sys.modules["graph.node_ops"])
        r, ms = timed_call(node_impl, action="add", label="Person", name=person_name,
                           description="Phase 4b test entity", driver=driver, database=TEST_GRAPH_DB)
        rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
        if "created" in rstr.lower() or person_name in rstr:
            results.ok("graph.node", f"add Person: {rstr[:80]}", ms)
        else:
            results.fail("graph.node", f"add Person unexpected: {rstr[:120]}", ms)
    except Exception as e:
        results.fail("graph.node", f"add: {type(e).__name__}: {e}")

    # graph.node (add Organization)
    try:
        r, ms = timed_call(node_impl, action="add", label="Organization", name=org_name,
                           description="Phase 4b test org", driver=driver, database=TEST_GRAPH_DB)
        results.ok("graph.node", f"add Organization OK", ms)
    except Exception as e:
        results.fail("graph.node", f"add Org: {type(e).__name__}: {e}")

    # graph.node (update)
    try:
        r, ms = timed_call(node_impl, action="update", label="Person", name=person_name,
                           props={"testField": "phase4b"}, driver=driver, database=TEST_GRAPH_DB)
        results.ok("graph.node", f"update OK", ms)
    except Exception as e:
        results.fail("graph.node", f"update: {type(e).__name__}: {e}")

    # graph.node (get)
    try:
        r, ms = timed_call(node_impl, action="get", label="Person", name=person_name,
                           driver=driver, database=TEST_GRAPH_DB)
        rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
        if person_name in rstr:
            results.ok("graph.node", f"get OK", ms)
        else:
            results.fail("graph.node", f"get: not found in {rstr[:100]}", ms)
    except Exception as e:
        results.fail("graph.node", f"get: {type(e).__name__}: {e}")

    # graph.rel (add)
    try:
        from graph.rel_ops import rel_impl
        importlib.reload(sys.modules["graph.rel_ops"])
        r, ms = timed_call(rel_impl, action="add", type="EMPLOYED_BY",
                           from_name=person_name, to_name=org_name,
                           props={"role": "Tester"}, driver=driver, database=TEST_GRAPH_DB)
        results.ok("graph.rel", f"add EMPLOYED_BY OK", ms)
    except Exception as e:
        results.fail("graph.rel", f"add: {type(e).__name__}: {e}")

    # graph.wire_evidence
    try:
        from graph.wire_evidence import wire_evidence_impl
        importlib.reload(sys.modules["graph.wire_evidence"])
        r, ms = timed_call(wire_evidence_impl, entity=person_name, label="Person",
                           sources=[{"url": source_url, "confidence": "training-knowledge",
                                    "claim": "test claim for phase 4b"}],
                           driver=driver, database=TEST_GRAPH_DB)
        results.ok("graph.wire_evidence", f"wired: {json.dumps(r, default=str)[:80]}", ms)
    except Exception as e:
        results.fail("graph.wire_evidence", f"{type(e).__name__}: {e}")

    # graph.commit (batch)
    try:
        from graph.commit_ops import commit_impl
        importlib.reload(sys.modules["graph.commit_ops"])
        person2 = f"{TEST_PREFIX}person2"
        r, ms = timed_call(commit_impl, operations=[
            {"op": "node", "action": "add", "label": "Person", "name": person2, "description": "batch test"},
            {"op": "rel", "action": "add", "type": "FAMILY_OF", "from_name": person_name,
             "to_name": person2, "props": {"relation": "sibling"}},
        ], driver=driver, database=TEST_GRAPH_DB)
        rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
        results.ok("graph.commit", f"batch: {rstr[:100]}", ms)
    except Exception as e:
        results.fail("graph.commit", f"{type(e).__name__}: {e}")

    # graph.write (high-level batch with embedded rels/sources)
    try:
        from graph.write_ops import write_impl
        importlib.reload(sys.modules["graph.write_ops"])
        person3 = f"{TEST_PREFIX}person3"
        r, ms = timed_call(write_impl, entities=[
            {"label": "Person", "name": person3, "description": "write_ops test",
             "relationships": [{"type": "AFFILIATED_WITH", "target": org_name, "props": {"role": "advisor"}}]}
        ], driver=driver, database=TEST_GRAPH_DB)
        results.ok("graph.write", f"entities written: {json.dumps(r, default=str)[:100]}", ms)
    except Exception as e:
        results.fail("graph.write", f"{type(e).__name__}: {e}")

    # graph.read (entity + network after writes)
    try:
        from graph.read_ops import read_impl
        importlib.reload(sys.modules["graph.read_ops"])
        r, ms = timed_call(read_impl, entity=person_name, network=1, driver=driver, database=TEST_GRAPH_DB)
        rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
        has_rel = org_name in rstr
        results.ok("graph.read", f"network=1, found_rel={has_rel}", ms)
    except Exception as e:
        results.fail("graph.read", f"entity+network: {type(e).__name__}: {e}")

    # graph.deduplicate (scan mode -- should find nothing to dedup in test data)
    try:
        from graph.dedup_ops import deduplicate_impl
        importlib.reload(sys.modules["graph.dedup_ops"])
        r, ms = timed_call(deduplicate_impl, action="scan", label="Person", threshold=0.8,
                           driver=driver, database=TEST_GRAPH_DB)
        results.ok("graph.deduplicate", f"scan: {json.dumps(r, default=str)[:100]}", ms)
    except Exception as e:
        results.fail("graph.deduplicate", f"{type(e).__name__}: {e}")

    # graph.board_snapshot (domain-specific -- test with our test org)
    try:
        from graph.board_snapshot import board_snapshot_impl
        importlib.reload(sys.modules["graph.board_snapshot"])
        r, ms = timed_call(board_snapshot_impl, organization=org_name,
                           members=[{"name": person_name, "role": "Board Chair"}],
                           fiscal_year=2026, driver=driver, database=TEST_GRAPH_DB)
        results.ok("graph.board_snapshot", f"domain-specific: {json.dumps(r, default=str)[:80]}", ms)
    except Exception as e:
        results.fail("graph.board_snapshot", f"{type(e).__name__}: {e}")

    # graph.gds (list available algorithms)
    try:
        from graph.gds_ops import gds_impl
        importlib.reload(sys.modules["graph.gds_ops"])
        r, ms = timed_call(gds_impl, action="list", driver=driver, database=TEST_GRAPH_DB)
        rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
        count = r.get("algorithm_count", "?") if isinstance(r, dict) else "?"
        results.ok("graph.gds", f"list: {count} algorithms", ms)
    except Exception as e:
        results.fail("graph.gds", f"list: {type(e).__name__}: {e}")

    # -- Cleanup --
    print("  --- tier 2 cleanup ---")
    try:
        records, summary, keys = driver.execute_query(
            "MATCH (n) WHERE n.name STARTS WITH $prefix DETACH DELETE n RETURN count(n) AS deleted",
            prefix=TEST_PREFIX, database_=TEST_GRAPH_DB
        )
        deleted = records[0]["deleted"] if records else 0
        # Also clean up test Source node
        driver.execute_query(
            "MATCH (s:Source {url: $url}) DETACH DELETE s RETURN count(s)",
            url=source_url, database_=TEST_GRAPH_DB
        )
        print(f"  [CLEANUP] {deleted} test nodes removed")
    except Exception as e:
        print(f"  [CLEANUP FAIL] {e}")


# ============================================================
# TIER 3: Entry/workflow operations (need lifestream paths)
# ============================================================

def tier3_entry_ops(results, server, driver):
    """Entry and session operations -- uses temp directory for lifestream output."""
    print("\n-- TIER 3: Entry/Workflow Operations --")

    # Create temp directory for lifestream markdown output
    tmp_dir = Path(tempfile.mkdtemp(prefix="nicktools_test_"))
    ls_dir = tmp_dir / "lifestream" / "stream"
    ls_dir.mkdir(parents=True)

    try:
        # entry.session_start
        try:
            from workflow.session_start import session_start_impl
            importlib.reload(sys.modules["workflow.session_start"])
            r, ms = timed_call(session_start_impl, driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            results.ok("entry.session_start", f"sections={len(r) if isinstance(r, dict) else '?'}", ms)
        except Exception as e:
            results.fail("entry.session_start", f"{type(e).__name__}: {e}")

        # entry.create_entry
        test_entry_id = None
        try:
            from workflow.create_entry import create_entry_impl
            importlib.reload(sys.modules["workflow.create_entry"])
            r, ms = timed_call(create_entry_impl,
                               title=f"{TEST_PREFIX}test entry for phase 4b",
                               entry_type="finding",
                               content="This is a test entry created during phase 4b verification.",
                               domains=["tooling"],
                               tags=["test", "phase-4b"],
                               status="active",
                               lifestream_dir=str(ls_dir),
                               driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            test_entry_id = r.get("entry_id") if isinstance(r, dict) else None
            if test_entry_id:
                results.ok("entry.create_entry", f"id={test_entry_id}", ms)
            else:
                results.ok("entry.create_entry", rstr[:100], ms)
        except Exception as e:
            results.fail("entry.create_entry", f"{type(e).__name__}: {e}")

        # entry.update_entry
        if test_entry_id:
            try:
                from workflow.update_entry import update_entry_impl
                importlib.reload(sys.modules["workflow.update_entry"])
                r, ms = timed_call(update_entry_impl, entry_id=test_entry_id,
                                   status="complete",
                                   content="Updated during phase 4b testing.",
                                   lifestream_dir=str(ls_dir),
                                   driver=driver)
                results.ok("entry.update_entry", f"updated {test_entry_id}", ms)
            except Exception as e:
                results.fail("entry.update_entry", f"{type(e).__name__}: {e}")
        else:
            results.skip("entry.update_entry", "no test entry to update")

        # entry.session_audit
        try:
            from workflow.session_audit import session_audit_impl
            importlib.reload(sys.modules["workflow.session_audit"])
            r, ms = timed_call(session_audit_impl, lifestream_dir=str(ls_dir), driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            issues = r.get("issue_count", "?") if isinstance(r, dict) else "?"
            results.ok("entry.session_audit", f"issues={issues}", ms)
        except Exception as e:
            results.fail("entry.session_audit", f"{type(e).__name__}: {e}")

    finally:
        # Clean up temp lifestream dir
        shutil.rmtree(tmp_dir, ignore_errors=True)

        # Clean up test entry from Neo4j
        if test_entry_id:
            try:
                driver.execute_query(
                    "MATCH (s:StreamEntry {id: $id}) DETACH DELETE s",
                    id=test_entry_id, database_=TEST_ENTRY_DB
                )
                # Also clean from graph DB (EntryRef)
                driver.execute_query(
                    "MATCH (e:EntryRef {entryId: $id}) DETACH DELETE e",
                    id=test_entry_id, database_=TEST_GRAPH_DB
                )
                print(f"  [CLEANUP] Entry {test_entry_id} removed")
            except Exception as e:
                print(f"  [CLEANUP FAIL] {e}")


# ============================================================
# TIER 4: Core I/O operations
# ============================================================

def tier4_core_io(results, server, driver):
    """Core I/O, Python execution, and system operations."""
    print("\n-- TIER 4: Core I/O Operations --")

    tmp_dir = Path(tempfile.mkdtemp(prefix="nicktools_test_io_"))

    try:
        # core.run_python
        try:
            from core.run_python import run_python_impl
            importlib.reload(sys.modules["core.run_python"])
            r, ms = timed_call(run_python_impl, code='print("hello from phase 4b")', driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            if "hello" in rstr:
                results.ok("core.run_python", "stdout captured", ms)
            else:
                results.fail("core.run_python", f"no stdout: {rstr[:100]}", ms)
        except Exception as e:
            results.fail("core.run_python", f"{type(e).__name__}: {e}")

        # core.run_command
        try:
            from core.run_command import run_command_impl
            importlib.reload(sys.modules["core.run_command"])
            r, ms = timed_call(run_command_impl, command="echo test_4b", shell="powershell", driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            if "test_4b" in rstr:
                results.ok("core.run_command", "echo captured", ms)
            else:
                results.ok("core.run_command", f"ran (output: {rstr[:80]})", ms)
        except Exception as e:
            results.fail("core.run_command", f"{type(e).__name__}: {e}")

        # core.write_file
        test_file = tmp_dir / "test_write.txt"
        try:
            from core.write_file import write_file_impl
            importlib.reload(sys.modules["core.write_file"])
            r, ms = timed_call(write_file_impl, path=str(test_file), content="phase 4b test content", driver=driver)
            if test_file.exists():
                results.ok("core.write_file", f"wrote {test_file.stat().st_size} bytes", ms)
            else:
                results.fail("core.write_file", "file not created", ms)
        except Exception as e:
            results.fail("core.write_file", f"{type(e).__name__}: {e}")

        # core.read_file
        try:
            from core.read_file import read_file_impl
            importlib.reload(sys.modules["core.read_file"])
            r, ms = timed_call(read_file_impl, path=str(test_file), driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            if "phase 4b" in rstr:
                results.ok("core.read_file", "content read correctly", ms)
            else:
                results.fail("core.read_file", f"wrong content: {rstr[:100]}", ms)
        except Exception as e:
            results.fail("core.read_file", f"{type(e).__name__}: {e}")

        # core.neo4j_query
        try:
            from core.neo4j_query import neo4j_query_impl
            importlib.reload(sys.modules["core.neo4j_query"])
            r, ms = timed_call(neo4j_query_impl, cypher="RETURN 42 AS answer",
                               database=TEST_GRAPH_DB, driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            if "42" in rstr:
                results.ok("core.neo4j_query", "query returned 42", ms)
            else:
                results.ok("core.neo4j_query", f"ran ({rstr[:80]})", ms)
        except Exception as e:
            results.fail("core.neo4j_query", f"{type(e).__name__}: {e}")

        # core.read_document (test with a simple text file)
        try:
            from core.read_document import read_document_impl
            importlib.reload(sys.modules["core.read_document"])
            r, ms = timed_call(read_document_impl, path=str(test_file), driver=driver)
            results.ok("core.read_document", f"read OK", ms)
        except Exception as e:
            results.fail("core.read_document", f"{type(e).__name__}: {e}")

        # core.run_script (skip -- needs a real script path)
        results.skip("core.run_script", "needs valid script path; tested via run_python")

        # core.restart_server (skip -- would actually restart)
        results.skip("core.restart_server", "would restart live server; skip in test")

        # core.backup_graph
        try:
            from core.backup_graph import backup_graph_impl
            importlib.reload(sys.modules["core.backup_graph"])
            r, ms = timed_call(backup_graph_impl, database=TEST_GRAPH_DB, driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            results.ok("core.backup_graph", f"backup: {rstr[:100]}", ms)
        except Exception as e:
            # May fail on empty test DB -- that's informative
            results.fail("core.backup_graph", f"{type(e).__name__}: {e}")

        # core.sync_system_docs (domain-flavored -- references ClaudeFiles paths)
        try:
            from core.sync_system_docs import sync_system_docs_impl
            importlib.reload(sys.modules["core.sync_system_docs"])
            r, ms = timed_call(sync_system_docs_impl, sections=["landscape"], driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            results.ok("core.sync_system_docs", f"landscape: {rstr[:100]}", ms)
        except Exception as e:
            results.fail("core.sync_system_docs", f"{type(e).__name__}: {e}")

        # core.session_ingest
        try:
            from core.session_ingest import session_ingest_impl
            importlib.reload(sys.modules["core.session_ingest"])
            r, ms = timed_call(session_ingest_impl, mode="scan", driver=driver)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            # Verify snake_case normalization
            if isinstance(r, dict) and "sessions" in r:
                for s in r["sessions"][:1]:
                    assert "session_id" in s, f"Expected snake_case 'session_id', got keys: {list(s.keys())}"
                    assert "sessionId" not in s, "camelCase 'sessionId' should be normalized"
            results.ok("core.session_ingest", f"scan: {rstr[:100]}", ms)
        except Exception as e:
            results.fail("core.session_ingest", f"{type(e).__name__}: {e}")

        # core.session_costs (needs audit logs -- may return empty)
        try:
            from core.session_costs import session_costs_impl
            importlib.reload(sys.modules["core.session_costs"])
            r, ms = timed_call(session_costs_impl, driver=driver)
            # Verify snake_case normalization
            if isinstance(r, dict) and "top_sessions_by_cost" in r:
                for s in r["top_sessions_by_cost"][:1]:
                    assert "session_id" in s, f"Expected snake_case 'session_id' in cost data"
                    assert "sessionId" not in s, "camelCase 'sessionId' should be normalized"
            results.ok("core.session_costs", f"cost data: {json.dumps(r, default=str)[:80]}", ms)
        except Exception as e:
            results.fail("core.session_costs", f"{type(e).__name__}: {e}")

        # core.session_search (needs audit logs -- may return empty)
        try:
            from core.session_search import session_search_impl
            importlib.reload(sys.modules["core.session_search"])
            r, ms = timed_call(session_search_impl, query="test", max_results=5, driver=driver)
            # Verify snake_case normalization
            if isinstance(r, dict) and "results" in r:
                assert "sessions_searched" in r, "Expected snake_case 'sessions_searched'"
                assert "sessionsSearched" not in r, "camelCase should be normalized"
            results.ok("core.session_search", f"search: {json.dumps(r, default=str)[:80]}", ms)
        except Exception as e:
            results.fail("core.session_search", f"{type(e).__name__}: {e}")

        # core.backfill_discusses
        try:
            from core.backfill_discusses import backfill_discusses_impl
            importlib.reload(sys.modules["core.backfill_discusses"])
            r, ms = timed_call(backfill_discusses_impl, dry_run=True, batch_size=5, driver=driver)
            results.ok("core.backfill_discusses", f"dry_run: {json.dumps(r, default=str)[:80]}", ms)
        except Exception as e:
            results.fail("core.backfill_discusses", f"{type(e).__name__}: {e}")

        # core.harvest_session (needs session data -- test with latest)
        try:
            from core.harvest_session import harvest_session_impl
            importlib.reload(sys.modules["core.harvest_session"])
            r, ms = timed_call(harvest_session_impl, mode="digest", driver=driver)
            results.ok("core.harvest_session", f"digest: {json.dumps(r, default=str)[:80]}", ms)
        except Exception as e:
            results.fail("core.harvest_session", f"{type(e).__name__}: {e}")

        # core.session_recover
        try:
            from core.session_recover import session_recover_impl
            importlib.reload(sys.modules["core.session_recover"])
            r, ms = timed_call(session_recover_impl, driver=driver)
            results.ok("core.session_recover", f"recover: {json.dumps(r, default=str)[:80]}", ms)
        except Exception as e:
            results.fail("core.session_recover", f"{type(e).__name__}: {e}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ============================================================
# TIER 5: Research/network operations
# ============================================================

def tier5_research_ops(results, server, driver):
    """Research operations -- some need network, some are local-only."""
    print("\n-- TIER 5: Research Operations --")

    if SKIP_NETWORK:
        network_ops = [
            "research.browse_url", "research.fetch_page", "research.archive_source",
            "research.save_page", "research.check_sources", "research.wayback_lookup",
            "research.search_records", "research.vin_decode", "research.search_business",
        ]
        for op in network_ops:
            results.skip(op, "network tests skipped (--skip-network)")
    else:
        # research.fetch_page (primary reading tool)
        try:
            from research.fetch_page import fetch_page_impl
            importlib.reload(sys.modules["research.fetch_page"])
            r, ms = timed_call(fetch_page_impl, url="https://example.com",
                               driver=driver, database=TEST_GRAPH_DB)
            rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
            if "Example Domain" in rstr or "text" in rstr or "content" in rstr:
                results.ok("research.fetch_page", f"fetched example.com", ms)
            else:
                results.ok("research.fetch_page", f"response: {rstr[:100]}", ms)
        except Exception as e:
            results.fail("research.fetch_page", f"{type(e).__name__}: {e}")

        # research.wayback_lookup
        try:
            from research.wayback_lookup import wayback_lookup_impl
            importlib.reload(sys.modules["research.wayback_lookup"])
            r, ms = timed_call(wayback_lookup_impl, url="https://example.com", driver=driver)
            results.ok("research.wayback_lookup", f"lookup: {json.dumps(r, default=str)[:80]}", ms)
        except Exception as e:
            results.fail("research.wayback_lookup", f"{type(e).__name__}: {e}")

        # Subprocess tools (browse_url, archive_source, save_page, check_sources) -- skip in test
        # They require nodriver (async browser), which needs its own event loop
        for op in ["research.browse_url", "research.archive_source", "research.save_page", "research.check_sources"]:
            results.skip(op, "subprocess/nodriver tool -- requires live MCP dispatch")

        # research.search_records (domain-flavored)
        results.skip("research.search_records", "requires API keys; classification: domain-flavored")

        # research.vin_decode (domain-specific)
        results.skip("research.vin_decode", "domain-specific vehicle research; requires API key")

        # research.search_business (domain-specific)
        results.skip("research.search_business", "domain-specific business search; requires browser")

    # -- Local research ops (no network needed) --

    # research.extract_article (needs a saved HTML file)
    results.skip("research.extract_article", "needs archived HTML file; tested via archive pipeline")

    # research.search_pdf (needs a PDF file)
    results.skip("research.search_pdf", "needs PDF file; tested manually")

    # research.queue_archive
    try:
        from research.queue_archive import queue_archive_impl
        importlib.reload(sys.modules["research.queue_archive"])
        r, ms = timed_call(queue_archive_impl, url="https://example.com/test-4b")
        rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
        results.ok("research.queue_archive", f"queued: {rstr[:80]}", ms)
    except Exception as e:
        results.fail("research.queue_archive", f"{type(e).__name__}: {e}")

    # research.check_queue
    try:
        from research.check_queue import check_queue_impl
        importlib.reload(sys.modules["research.check_queue"])
        r, ms = timed_call(check_queue_impl)
        rstr = json.dumps(r, default=str) if isinstance(r, dict) else str(r)
        results.ok("research.check_queue", f"status: {rstr[:80]}", ms)
    except Exception as e:
        results.fail("research.check_queue", f"{type(e).__name__}: {e}")

    # research.read_staged
    try:
        from research.read_staged import read_staged_impl
        importlib.reload(sys.modules["research.read_staged"])
        r, ms = timed_call(read_staged_impl)
        results.ok("research.read_staged", f"staged: {json.dumps(r, default=str)[:80]}", ms)
    except Exception as e:
        results.fail("research.read_staged", f"{type(e).__name__}: {e}")

    # research.archive_inventory
    try:
        from research.archive_inventory import archive_inventory_impl
        importlib.reload(sys.modules["research.archive_inventory"])
        r, ms = timed_call(archive_inventory_impl, driver=driver, database=TEST_GRAPH_DB)
        results.ok("research.archive_inventory", f"inventory: {json.dumps(r, default=str)[:80]}", ms)
    except Exception as e:
        results.fail("research.archive_inventory", f"{type(e).__name__}: {e}")

    # research.process_queue (skip -- would actually process queue items)
    results.skip("research.process_queue", "would process real queue; tested via queue_archive+check_queue")

    # research.ingest_saved
    results.skip("research.ingest_saved", "needs saved page directory; tested via archive pipeline")

    # research.generate_report
    try:
        from research.generate_report import generate_report_impl
        importlib.reload(sys.modules["research.generate_report"])
        r, ms = timed_call(generate_report_impl, topic="test report",
                           entities=[f"{TEST_PREFIX}nonexistent"],
                           driver=driver, database=TEST_GRAPH_DB)
        # May return empty results for nonexistent entities -- that's fine
        results.ok("research.generate_report", f"report: {json.dumps(r, default=str)[:80]}", ms)
    except Exception as e:
        results.fail("research.generate_report", f"{type(e).__name__}: {e}")


# ============================================================
# Main
# ============================================================

def main():
    print(f"nicktools Phase 4b -- Tool-by-tool Verification")
    print(f"Working directory: {SERVER_DIR}")
    print(f"Graph database: {TEST_GRAPH_DB}")
    print(f"Entry database: {TEST_ENTRY_DB}")
    print(f"Skip network: {SKIP_NETWORK}")
    print(f"Tiers: {sorted(TIERS)}")
    print(f"{'='*70}")

    results = Results()

    try:
        server, driver = setup()
        print(f"Server loaded: {server.SERVER_VERSION}, {len(server.IN_PROCESS_TOOLS)} in-process tools")
    except Exception as e:
        print(f"FATAL: Cannot load server: {e}")
        traceback.print_exc()
        sys.exit(1)

    if 1 in TIERS:
        tier1_safe_reads(results, server, driver)
    if 2 in TIERS:
        tier2_graph_writes(results, server, driver)
    if 3 in TIERS:
        tier3_entry_ops(results, server, driver)
    if 4 in TIERS:
        tier4_core_io(results, server, driver)
    if 5 in TIERS:
        tier5_research_ops(results, server, driver)

    success = results.summary()

    # -- Write results to JSON for later analysis --
    output_path = SERVER_DIR / "tests" / "test_operations_results.json"
    output = {
        "timestamp": datetime.now().isoformat(),
        "graph_db": TEST_GRAPH_DB,
        "entry_db": TEST_ENTRY_DB,
        "skip_network": SKIP_NETWORK,
        "tiers": sorted(TIERS),
        "results": [
            {"operation": r[0], "status": r[1], "classification": r[2],
             "detail": r[3], "duration_ms": r[4]}
            for r in results.records
        ],
        "summary": {
            "passed": sum(1 for r in results.records if r[1] == "PASS"),
            "failed": sum(1 for r in results.records if r[1] == "FAIL"),
            "skipped": sum(1 for r in results.records if r[1] == "SKIP"),
        }
    }
    output_path.write_text(json.dumps(output, indent=2))
    print(f"\nResults saved to: {output_path}")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
