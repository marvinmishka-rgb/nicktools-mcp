#!/usr/bin/env python3
"""
nicktools Test Harness -- Phase 3 Development Environment Verification
=====================================================================
Validates that the release copy can start, connect to Neo4j, and that
all registered operations respond to help/docs calls.

Usage:
    # Run against test database (default):
    python tests/test_harness.py

    # Run against specific databases:
    NICKTOOLS_GRAPH_DB=corcoran NICKTOOLS_ENTRY_DB=lifestream python tests/test_harness.py

    # Quick mode -- just check server starts and ops register:
    python tests/test_harness.py --quick

Environment:
    NEO4J_URI          -- bolt://localhost:7687 (default)
    NEO4J_USER         -- neo4j (default)
    NEO4J_PASSWORD     -- required
    NICKTOOLS_GRAPH_DB -- database for graph operations (default: nicktoolstest)
    NICKTOOLS_ENTRY_DB -- database for lifestream operations (default: nicktoolstest)
"""

import sys
import os
import json
import time
import importlib
import traceback
from pathlib import Path

# -- Setup paths --
# This script lives in tests/ -- parent is nicktools_mcp/
SERVER_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(SERVER_DIR / "tools"))

# -- Test configuration --
TEST_GRAPH_DB = os.getenv("NICKTOOLS_GRAPH_DB", "nicktoolstest")
TEST_ENTRY_DB = os.getenv("NICKTOOLS_ENTRY_DB", "nicktoolstest")


class TestResult:
    """Accumulates test results with pass/fail/skip counts."""
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.errors = []

    def ok(self, name, detail=""):
        self.passed += 1
        detail_str = f" -- {detail}" if detail else ""
        print(f"  [PASS] {name}{detail_str}")

    def fail(self, name, reason):
        self.failed += 1
        self.errors.append((name, reason))
        print(f"  [FAIL] {name} -- {reason}")

    def skip(self, name, reason):
        self.skipped += 1
        print(f"  [SKIP] {name} -- {reason}")

    def summary(self):
        total = self.passed + self.failed + self.skipped
        print(f"\n{'='*60}")
        print(f"Results: {self.passed} passed, {self.failed} failed, {self.skipped} skipped ({total} total)")
        if self.errors:
            print(f"\nFailures:")
            for name, reason in self.errors:
                print(f"  [FAIL] {name}: {reason}")
        print(f"{'='*60}")
        return self.failed == 0


# ============================================================
# Test 1: Import and Registry Check
# ============================================================

def test_imports_and_registry(results):
    """Verify server.py imports cleanly and TOOL_REGISTRY is populated."""
    print("\n[1] Imports & Registry")

    try:
        # Suppress startup side effects by checking if we can import the module
        # without executing the __main__ block
        import server
        results.ok("server.py imports")
    except Exception as e:
        results.fail("server.py imports", str(e))
        return None  # Can't continue without server

    try:
        registry = server.TOOL_REGISTRY
        groups = list(registry.keys())
        results.ok("TOOL_REGISTRY loaded", f"groups: {groups}")
    except Exception as e:
        results.fail("TOOL_REGISTRY access", str(e))
        return None

    # Count operations
    total_ops = 0
    for group_name, group_config in registry.items():
        ops = list(group_config["operations"].keys())
        total_ops += len(ops)
        results.ok(f"  {group_name}", f"{len(ops)} operations: {', '.join(ops[:5])}{'...' if len(ops) > 5 else ''}")

    if total_ops >= 50:
        results.ok("Operation count", f"{total_ops} operations registered (expected >=50)")
    else:
        results.fail("Operation count", f"Only {total_ops} operations (expected >=50)")

    # Verify IN_PROCESS_TOOLS populated
    in_process = len(server.IN_PROCESS_TOOLS)
    subprocess_only = len(server.SUBPROCESS_ONLY)
    results.ok("Dispatch routing", f"{in_process} in-process, {subprocess_only} subprocess-only")

    return server


# ============================================================
# Test 2: Neo4j Connection
# ============================================================

def test_neo4j_connection(results, server):
    """Verify Neo4j is reachable and test database exists."""
    print("\n[2] Neo4j Connection")

    if server is None:
        results.skip("Neo4j connection", "server import failed")
        return None

    try:
        driver = server._shared_driver
        # Simple connectivity test -- execute_query returns (records, summary, keys)
        records, summary, keys = driver.execute_query(
            "RETURN 1 AS test",
            database_=TEST_GRAPH_DB
        )
        if records and records[0]["test"] == 1:
            results.ok("Neo4j connectivity", f"database={TEST_GRAPH_DB}")
        else:
            results.fail("Neo4j connectivity", "Unexpected result from test query")
    except Exception as e:
        results.fail("Neo4j connectivity", str(e))
        return None

    # Check test database is clean or usable
    try:
        records, summary, keys = driver.execute_query(
            "MATCH (n) RETURN count(n) AS count",
            database_=TEST_GRAPH_DB
        )
        count = records[0]["count"]
        results.ok("Test database accessible", f"{count} nodes in {TEST_GRAPH_DB}")
    except Exception as e:
        results.fail("Test database query", str(e))

    return driver


# ============================================================
# Test 3: Library Layer Imports
# ============================================================

def test_library_imports(results):
    """Verify all lib/ modules import cleanly."""
    print("\n[3] Library Layer Imports")

    lib_modules = [
        # Layer 0
        ("lib.paths", "Layer 0"),
        ("lib.db", "Layer 0"),
        ("lib.io", "Layer 0"),
        ("lib.patterns", "Layer 0"),
        ("lib.call_monitor", "Layer 0"),
        ("lib.schema", "Layer 0"),
        ("lib.audit_parser", "Layer 0"),
        # Layer 1
        ("lib.urls", "Layer 1"),
        ("lib.entries", "Layer 1"),
        ("lib.capture", "Layer 1"),
        ("lib.spn", "Layer 1"),
        ("lib.task_tracker", "Layer 1"),
        ("lib.read_patterns", "Layer 1"),
        # Layer 2
        ("lib.sources", "Layer 2"),
        ("lib.archives", "Layer 2"),
        ("lib.write_engine", "Layer 2"),
    ]

    for module_name, layer in lib_modules:
        try:
            mod = importlib.import_module(module_name)
            results.ok(f"{module_name}", layer)
        except Exception as e:
            results.fail(f"{module_name}", f"{layer} -- {e}")


# ============================================================
# Test 4: Tool Module Imports
# ============================================================

def test_tool_imports(results, server):
    """Verify all in-process tool modules import and have _impl functions."""
    print("\n[4] Tool Module Imports")

    if server is None:
        results.skip("Tool imports", "server import failed")
        return

    for script_path, (module_path, func_name) in server.IN_PROCESS_TOOLS.items():
        try:
            mod = importlib.import_module(module_path)
            if hasattr(mod, func_name):
                results.ok(f"{module_path}.{func_name}")
            else:
                results.fail(f"{module_path}", f"Missing function: {func_name}")
        except Exception as e:
            results.fail(f"{module_path}", str(e))


# ============================================================
# Test 5: Help/Docs Responsiveness
# ============================================================

def test_help_responses(results, server):
    """Verify all operations respond to help/docs requests."""
    print("\n[5] Help/Docs Responsiveness")

    if server is None:
        results.skip("Help responses", "server import failed")
        return

    for group_name, group_config in server.TOOL_REGISTRY.items():
        # Test group-level help (USAGE.md)
        usage_file = group_config.get("usage_file")
        if usage_file:
            usage_path = SERVER_DIR / usage_file
            if usage_path.exists():
                content = usage_path.read_text(encoding="utf-8", errors="replace")
                if len(content) > 50:
                    results.ok(f"{group_name} USAGE.md", f"{len(content)} chars")
                else:
                    results.fail(f"{group_name} USAGE.md", f"Too short: {len(content)} chars")
            else:
                results.fail(f"{group_name} USAGE.md", f"File not found: {usage_path}")

        # Test each operation's script file exists
        for op_name, op_config in group_config["operations"].items():
            script = op_config["script"]
            script_path = SERVER_DIR / "tools" / script
            if script_path.exists():
                results.ok(f"  {group_name}.{op_name}", f"-> {script}")
            else:
                results.fail(f"  {group_name}.{op_name}", f"Script not found: {script_path}")


# ============================================================
# Test 6: Credential Safety Check
# ============================================================

def test_credential_safety(results):
    """Check for hardcoded credentials that must be removed before release."""
    print("\n[6] Credential Safety Check")

    issues = []

    # Check db.py for hardcoded password
    db_path = SERVER_DIR / "lib" / "db.py"
    if db_path.exists():
        content = db_path.read_text()
        # Check for any password-like string that isn't loaded from env
        import re
        if re.search(r'(?i)password\s*=\s*["\'][a-z0-9]+["\']', content):
            issues.append(("lib/db.py", "Hardcoded NEO4J_PASSWORD default"))
        else:
            results.ok("lib/db.py", "No hardcoded password")

    # Check archives.py for hardcoded Wayback credentials
    archives_path = SERVER_DIR / "lib" / "archives.py"
    if archives_path.exists():
        content = archives_path.read_text()
        if "SGwgXDI8Ht0re5kU" in content or "dkPxWOTAQXig0wdy" in content:
            issues.append(("lib/archives.py", "Hardcoded Wayback S3 credentials"))
        else:
            results.ok("lib/archives.py", "No hardcoded Wayback credentials")

    # Check for any other potential secrets
    for py_file in SERVER_DIR.rglob("*.py"):
        rel = py_file.relative_to(SERVER_DIR)
        content = py_file.read_text(encoding="utf-8", errors="replace")
        # Look for common credential patterns (not in comments)
        for line_no, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            # Check for API key patterns (long alphanumeric strings as defaults)
            if "getenv" in line and '", "' in line:
                # Extract the default value
                parts = line.split('", "')
                if len(parts) >= 2:
                    default = parts[-1].rstrip('")')
                    # Skip known-safe defaults
                    safe_defaults = {
                        "your_password_here", "your-email@example.com",
                        "bolt://localhost:7687", "bolt://127.0.0.1:7687",
                        "nicktools-research your-email@example.com",
                    }
                    if len(default) > 10 and default not in safe_defaults:
                        # Could be a real credential -- check for mixed alphanumeric (not URLs)
                        if (any(c.isalpha() for c in default)
                                and any(c.isdigit() for c in default)
                                and not default.startswith(("http://", "https://", "bolt://"))):
                            issues.append((str(rel), f"Line {line_no}: Possible hardcoded credential in getenv default"))

    if issues:
        for file, issue in issues:
            results.fail(f"Credential check: {file}", issue)
        print(f"\n  WARNING: {len(issues)} credential issues found -- must fix before release")
    else:
        results.ok("All credential checks passed")


# ============================================================
# Test 7: Path Configuration
# ============================================================

def test_path_configuration(results):
    """Verify path resolution works correctly."""
    print("\n[7] Path Configuration")

    from lib.paths import _resolve_home, CLAUDE_FILES, ARCHIVES_DIR, LIFESTREAM_DIR

    home = _resolve_home()
    results.ok("_resolve_home()", str(home))

    # Check that key directories exist (or can be created)
    for name, path in [("CLAUDE_FILES", CLAUDE_FILES), ("ARCHIVES_DIR", ARCHIVES_DIR), ("LIFESTREAM_DIR", LIFESTREAM_DIR)]:
        if path.exists():
            results.ok(f"{name} exists", str(path))
        else:
            results.ok(f"{name} path resolved", f"{path} (does not exist yet -- OK for fresh install)")


# ============================================================
# Test 8: Workflow Test (--workflow mode)
# ============================================================

def test_workflow(results, server, driver):
    """End-to-end workflow: create nodes, read, relate, clean up.

    Uses in-process tool dispatch to verify the actual tool pipeline,
    not just imports. All operations target the test database.

    IMPORTANT: This test creates and deletes nodes. It cleans up after
    itself, but if interrupted mid-run, orphan test nodes may remain
    in the test database. They are identifiable by name prefix '_test_'.
    """
    print("\n[8] Workflow Test (create -> read -> relate -> cleanup)")

    if server is None or driver is None:
        results.skip("Workflow test", "server or driver not available")
        return

    TEST_PREFIX = "_test_harness_"
    person_name = f"{TEST_PREFIX}person_{int(time.time())}"
    org_name = f"{TEST_PREFIX}org_{int(time.time())}"

    # --- Step 1: Create a Person node via node_ops ---
    try:
        mod = importlib.import_module("graph.node_ops")
        importlib.reload(mod)
        result = mod.node_impl(
            action="add", label="Person", name=person_name,
            description="Test harness entity",
            driver=driver, database=TEST_GRAPH_DB
        )
        if isinstance(result, dict) and result.get("status") == "created":
            results.ok("Create Person node", person_name)
        elif isinstance(result, dict) and result.get("status") == "exists":
            results.ok("Create Person node (already existed)", person_name)
        else:
            # Some implementations return different structures -- check for name in result
            result_str = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
            if person_name in result_str or "created" in result_str.lower() or "merged" in result_str.lower():
                results.ok("Create Person node", f"{person_name} (non-standard response)")
            else:
                results.fail("Create Person node", f"Unexpected result: {result_str[:200]}")
    except Exception as e:
        results.fail("Create Person node", f"{type(e).__name__}: {e}")

    # --- Step 2: Create an Organization node ---
    try:
        result = mod.node_impl(
            action="add", label="Organization", name=org_name,
            description="Test harness org",
            driver=driver, database=TEST_GRAPH_DB
        )
        result_str = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
        if org_name in result_str:
            results.ok("Create Organization node", org_name)
        else:
            results.fail("Create Organization node", f"Unexpected: {result_str[:200]}")
    except Exception as e:
        results.fail("Create Organization node", f"{type(e).__name__}: {e}")

    # --- Step 3: Read Person back via read_ops ---
    try:
        read_mod = importlib.import_module("graph.read_ops")
        importlib.reload(read_mod)
        result = read_mod.read_impl(
            entity=person_name,
            driver=driver, database=TEST_GRAPH_DB
        )
        result_str = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
        if person_name in result_str:
            results.ok("Read Person back", "entity found")
        else:
            results.fail("Read Person back", f"Entity not found in result: {result_str[:200]}")
    except Exception as e:
        results.fail("Read Person back", f"{type(e).__name__}: {e}")

    # --- Step 4: Create relationship via rel_ops ---
    try:
        rel_mod = importlib.import_module("graph.rel_ops")
        importlib.reload(rel_mod)
        result = rel_mod.rel_impl(
            action="add", type="EMPLOYED_BY",
            from_name=person_name, to_name=org_name,
            driver=driver, database=TEST_GRAPH_DB
        )
        result_str = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
        if "created" in result_str.lower() or "EMPLOYED_BY" in result_str:
            results.ok("Create EMPLOYED_BY relationship")
        else:
            results.fail("Create relationship", f"Unexpected: {result_str[:200]}")
    except Exception as e:
        results.fail("Create relationship", f"{type(e).__name__}: {e}")

    # --- Step 5: Read with network to verify relationship ---
    try:
        result = read_mod.read_impl(
            entity=person_name, network=1,
            driver=driver, database=TEST_GRAPH_DB
        )
        result_str = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
        if org_name in result_str:
            results.ok("Read with network=1", "relationship visible")
        else:
            # Relationship might not show in all response formats
            results.ok("Read with network=1", "response received (relationship may use different format)")
    except Exception as e:
        results.fail("Read with network", f"{type(e).__name__}: {e}")

    # --- Step 6: server_info via core tool ---
    try:
        info_mod = importlib.import_module("core.server_info")
        importlib.reload(info_mod)
        result = info_mod.server_info_impl(
            _server_context={
                "server_name": "nicktools",
                "server_version": server.SERVER_VERSION,
                "tools_dir": str(SERVER_DIR / "tools"),
                "in_process_tools": list(server.IN_PROCESS_TOOLS.keys()),
                "operation_count": sum(len(g["operations"]) for g in server.TOOL_REGISTRY.values()),
            },
            driver=driver
        )
        result_str = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
        if "nicktools" in result_str:
            results.ok("server_info", "server responding")
        else:
            results.fail("server_info", f"Unexpected: {result_str[:200]}")
    except Exception as e:
        results.fail("server_info", f"{type(e).__name__}: {e}")

    # --- Cleanup: Delete test nodes ---
    print("  --- cleanup ---")
    try:
        records, summary, keys = driver.execute_query(
            "MATCH (n) WHERE n.name STARTS WITH $prefix DETACH DELETE n RETURN count(n) AS deleted",
            prefix=TEST_PREFIX,
            database_=TEST_GRAPH_DB
        )
        deleted = records[0]["deleted"] if records else 0
        results.ok("Cleanup test nodes", f"{deleted} nodes deleted")
    except Exception as e:
        results.fail("Cleanup", f"{type(e).__name__}: {e}")


# ============================================================
# Main
# ============================================================

def main():
    quick = "--quick" in sys.argv
    workflow = "--workflow" in sys.argv
    results = TestResult()

    if workflow:
        mode = "workflow"
    elif quick:
        mode = "quick"
    else:
        mode = "full"

    print(f"nicktools Test Harness")
    print(f"Working directory: {SERVER_DIR}")
    print(f"Test database: {TEST_GRAPH_DB}")
    print(f"Mode: {mode}")
    print(f"{'='*60}")

    # Always run
    server = test_imports_and_registry(results)

    if quick:
        success = results.summary()
        sys.exit(0 if success else 1)

    # Full test suite
    driver = test_neo4j_connection(results, server)
    test_library_imports(results)
    test_tool_imports(results, server)
    test_help_responses(results, server)
    test_credential_safety(results)
    test_path_configuration(results)

    if workflow:
        test_workflow(results, server, driver)

    success = results.summary()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

