#!/usr/bin/env python3
"""Phase 4h: Fresh Install Test

Tests the nicktools MCP server from a fresh venv against empty databases.
Two phases:
  1. Server self-test (--test flag) validates startup, registry, Neo4j connection
  2. Direct _impl() calls test tool behavior on empty/sparse databases

Usage:
    python tests/test_fresh_install.py

Requires:
    - .env pointing to empty test databases (nicktoolstest, nicktoolsentries)
    - Fresh venv at ../test-venv/ with requirements installed
    - Neo4j running with test databases created
"""
import sys
import os
import json
import subprocess
import importlib

# Configuration
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(BASE_DIR, 'nicktools_mcp')
if os.path.basename(BASE_DIR) == 'nicktools_mcp':
    SERVER_DIR = BASE_DIR
    BASE_DIR = os.path.dirname(BASE_DIR)

VENV_PYTHON = os.path.join(BASE_DIR, 'test-venv', 'Scripts', 'python.exe')
SERVER_SCRIPT = os.path.join(SERVER_DIR, 'server.py')

results = {'passed': 0, 'failed': 0, 'errors': [], 'findings': []}


def log(msg, level='INFO'):
    prefix = {'INFO': '[.]', 'PASS': '[OK]', 'FAIL': '[FAIL]', 'ERROR': '[ERR]'}
    print(f"{prefix.get(level, '[.]')} {msg}")


def record(name, passed, detail=None):
    if passed:
        log(f"{name}", 'PASS')
        results['passed'] += 1
    else:
        log(f"{name}: {detail}", 'FAIL')
        results['failed'] += 1
        results['errors'].append(f"{name}: {detail}")


def finding(msg):
    """Record a first-run UX issue."""
    results['findings'].append(msg)
    log(f"FINDING: {msg}", 'INFO')


def call_impl(module_path, func_name, params):
    """Import a tool module and call its _impl function, mirroring server dispatch."""
    mod = importlib.import_module(module_path)
    importlib.reload(mod)
    func = getattr(mod, func_name)
    # Inject shared driver
    from lib.db import get_neo4j_driver
    params['driver'] = get_neo4j_driver()
    result = func(**params)
    if isinstance(result, str):
        return json.loads(result)
    return result


# ============================================================
# Phase 1: Server self-test
# ============================================================
def test_self_test():
    log("Running server --test (self-test mode)...")
    env = os.environ.copy()
    env['PYTHONUTF8'] = '1'
    proc = subprocess.run(
        [VENV_PYTHON, SERVER_SCRIPT, '--test'],
        capture_output=True, text=True, timeout=60,
        cwd=SERVER_DIR, env=env
    )
    output = proc.stdout + proc.stderr
    if proc.returncode == 0:
        record('Server self-test (--test)', True)
        # Extract key stats
        for line in output.split('\n'):
            stripped = line.strip()
            if stripped and ('loaded' in stripped.lower() or 'operation' in stripped.lower()
                            or 'pass' in stripped.lower() or 'fail' in stripped.lower()):
                log(f"  {stripped}")
        return True
    else:
        record('Server self-test', False, f'exit code {proc.returncode}')
        for line in output.split('\n')[-15:]:
            if line.strip():
                log(f"  {line.strip()}", 'ERROR')
        return False


# ============================================================
# Phase 2: Empty database behavior via direct _impl() calls
# ============================================================
def test_empty_db():
    log("Testing tool behavior against empty databases...")

    # Ensure we're using test databases
    os.environ['NICKTOOLS_GRAPH_DB'] = 'nicktoolstest'
    os.environ['NICKTOOLS_ENTRY_DB'] = 'nicktoolsentries'

    if SERVER_DIR not in sys.path:
        sys.path.insert(0, SERVER_DIR)

    # Force lib.db to pick up the test database env vars
    import lib.db
    importlib.reload(lib.db)

    created_entry_id = None

    # --- server_info ---
    try:
        r = call_impl('tools.core.server_info', 'server_info_impl', {})
        ok = 'server' in str(r).lower() or 'version' in str(r).lower() or 'operations' in str(r).lower()
        record('server_info', ok, str(r)[:200])
    except Exception as e:
        record('server_info', False, str(e))

    # --- node add (Person) ---
    try:
        r = call_impl('tools.graph.node_ops', 'node_impl', {
            'action': 'add', 'label': 'Person',
            'name': 'Test User', 'description': 'Created during fresh install test'
        })
        ok = 'error' not in str(r).lower() or r.get('action') == 'add'
        record('node add (Person)', ok, str(r)[:200])
    except Exception as e:
        record('node add (Person)', False, str(e))

    # --- node add (Organization) ---
    try:
        r = call_impl('tools.graph.node_ops', 'node_impl', {
            'action': 'add', 'label': 'Organization',
            'name': 'Test Corp', 'description': 'Test organization'
        })
        ok = 'error' not in str(r).lower() or r.get('action') == 'add'
        record('node add (Organization)', ok, str(r)[:200])
    except Exception as e:
        record('node add (Organization)', False, str(e))

    # --- rel add ---
    try:
        r = call_impl('tools.graph.rel_ops', 'rel_impl', {
            'action': 'add', 'type': 'AFFILIATED_WITH',
            'from_name': 'Test User', 'to_name': 'Test Corp'
        })
        ok = 'error' not in str(r).lower() or r.get('action') == 'add'
        record('rel add', ok, str(r)[:200])
    except Exception as e:
        record('rel add', False, str(e))

    # --- read ---
    try:
        r = call_impl('tools.graph.read_ops', 'read_impl', {'entity': 'Test User'})
        ok = 'Test User' in str(r) or r.get('entity')
        record('graph read', ok, str(r)[:200])
    except Exception as e:
        record('graph read', False, str(e))

    # --- wire_evidence ---
    try:
        r = call_impl('tools.graph.wire_evidence', 'wire_evidence_impl', {
            'entity': 'Test User',
            'sources': [{'url': 'https://example.com/test', 'title': 'Test Source', 'tier': 'web-search'}]
        })
        ok = 'error' not in str(r).lower() or 'wired' in str(r).lower() or r.get('entity')
        record('wire_evidence', ok, str(r)[:200])
    except Exception as e:
        record('wire_evidence', False, str(e))

    # --- cypher (ad-hoc read) ---
    try:
        r = call_impl('tools.graph.cypher_passthrough', 'cypher_impl', {
            'query': 'MATCH (n) RETURN labels(n) AS labels, n.name AS name LIMIT 10',
            'mode': 'read'
        })
        ok = 'records' in str(r) or isinstance(r.get('records'), list)
        record('cypher read', ok, str(r)[:200])
    except Exception as e:
        record('cypher read', False, str(e))

    # --- create_entry (first entry in empty lifestream DB) ---
    try:
        r = call_impl('tools.workflow.create_entry', 'create_entry_impl', {
            'title': 'Fresh Install Test Entry',
            'type': 'finding',
            'content': 'Created during Phase 4h fresh install test.',
            'domains': ['tooling'],
            'tags': ['test', 'fresh-install']
        })
        created_entry_id = r.get('entry_id', '')
        ok = created_entry_id.startswith('ls-')
        record(f'create_entry -> {created_entry_id}', ok, str(r)[:200])
        if ok and created_entry_id == 'ls-20260304-001':
            finding('First entry in empty DB gets ls-YYYYMMDD-001 -- correct ID sequencing confirmed')
    except Exception as e:
        record('create_entry', False, str(e))

    # --- session_start (empty DB) ---
    try:
        r = call_impl('tools.workflow.session_start', 'session_start_impl', {})
        ok = isinstance(r, dict) and 'error' not in str(r.get('', '')).lower()
        record('session_start (empty DB)', ok, str(r)[:300])
        # Check if the output is useful for orientation
        if ok:
            keys = list(r.keys()) if isinstance(r, dict) else []
            if not keys or len(keys) < 2:
                finding('session_start returns minimal info on empty DB -- new user gets little orientation')
    except Exception as e:
        record('session_start', False, str(e))
        if 'division by zero' in str(e) or 'NoneType' in str(e):
            finding(f'session_start crashes on empty DB: {e}')

    # --- session_audit (sparse DB) ---
    try:
        r = call_impl('tools.workflow.session_audit', 'session_audit_impl', {})
        ok = isinstance(r, dict)
        record('session_audit (sparse DB)', ok, str(r)[:200])
    except Exception as e:
        record('session_audit', False, str(e))
        if 'division by zero' in str(e) or 'NoneType' in str(e):
            finding(f'session_audit crashes on sparse DB: {e}')

    # --- update_entry ---
    if created_entry_id:
        try:
            r = call_impl('tools.workflow.update_entry', 'update_entry_impl', {
                'entry_id': created_entry_id,
                'content': 'Updated during fresh install test.',
                'status': 'complete'
            })
            ok = 'error' not in str(r).lower()
            record('update_entry', ok, str(r)[:200])
        except Exception as e:
            record('update_entry', False, str(e))

    # --- deduplicate scan ---
    try:
        r = call_impl('tools.graph.dedup_ops', 'deduplicate_impl', {'action': 'scan'})
        ok = isinstance(r, dict)
        record('deduplicate scan', ok, str(r)[:200])
    except Exception as e:
        record('deduplicate scan', False, str(e))

    # --- commit (batch write) ---
    try:
        r = call_impl('tools.graph.commit_ops', 'commit_impl', {
            'operations': [
                {'op': 'node', 'action': 'add', 'label': 'Event', 'name': 'Test Event', 'event_type': 'other', 'description': 'Batch test'},
                {'op': 'rel', 'action': 'add', 'type': 'ATTENDED', 'from_name': 'Test User', 'to_name': 'Test Event'}
            ]
        })
        has_errors = bool(r.get('validation_errors'))
        any_created = any(res.get('created') for res in r.get('results', []))
        ok = not has_errors and (any_created or r.get('committed', 0) > 0)
        record('commit (batch)', ok, str(r)[:200])
    except Exception as e:
        record('commit (batch)', False, str(e))

    # --- write (batch entities) ---
    try:
        r = call_impl('tools.graph.write_ops', 'write_impl', {
            'entities': [{
                'label': 'Person',
                'name': 'Batch Test Person',
                'description': 'Created via write op',
                'relationships': [{'type': 'AFFILIATED_WITH', 'target': 'Test Corp'}]
            }]
        })
        ok = r.get('summary', {}).get('nodes_created', 0) > 0 or (isinstance(r.get('entities'), list) and any(e.get('created') for e in r['entities']))
        record('write (batch entities)', ok, str(r)[:200])
    except Exception as e:
        record('write (batch entities)', False, str(e))

    # --- phase (list, should work on empty project) ---
    try:
        from tools.workflow import phase_ops
        importlib.reload(phase_ops)
        from lib.db import get_neo4j_driver as get_driver
        r = phase_ops.phase_impl(action='list', project='test-project', driver=get_driver())
        if isinstance(r, str):
            r = json.loads(r)
        ok = isinstance(r, dict) and 'phases' in r
        record('phase list (empty project)', ok, str(r)[:200])
    except Exception as e:
        record('phase list', False, str(e))

    # --- Cleanup ---
    try:
        from lib.db import get_neo4j_driver as get_driver
        driver = get_driver()
        with driver.session(database='nicktoolstest') as session:
            result = session.run('MATCH (n) DETACH DELETE n RETURN count(n) AS deleted')
            deleted_graph = result.single()['deleted']
        with driver.session(database='nicktoolsentries') as session:
            result = session.run('MATCH (n) DETACH DELETE n RETURN count(n) AS deleted')
            deleted_entries = result.single()['deleted']
        log(f"  Cleanup: deleted {deleted_graph} graph nodes, {deleted_entries} entry nodes")
    except Exception as e:
        log(f"  Cleanup failed: {e}", 'ERROR')


def main():
    print("=" * 60)
    print("Phase 4h: Fresh Install Test")
    print("=" * 60)
    print(f"Venv Python: {VENV_PYTHON}")
    print(f"Server: {SERVER_SCRIPT}")
    print(f"Server dir: {SERVER_DIR}")
    print()

    if not os.path.exists(VENV_PYTHON):
        log(f"Venv not found: {VENV_PYTHON}", 'ERROR')
        sys.exit(1)

    # Phase 1
    print("--- Phase 1: Server Self-Test ---")
    self_test_ok = test_self_test()
    print()

    # Phase 2
    print("--- Phase 2: Empty Database Behavior ---")
    test_empty_db()
    print()

    # Summary
    print("=" * 60)
    print(f"Results: {results['passed']} passed, {results['failed']} failed")
    if results['findings']:
        print(f"\nFirst-run findings ({len(results['findings']):})")
        for i, f in enumerate(results['findings'], 1):
            print(f"  {i}. {f}")
    if results['errors']:
        print(f"\nErrors ({len(results['errors']):})")
        for e in results['errors']:
            print(f"  - {e}")
    print("=" * 60)

    sys.exit(0 if results['failed'] == 0 else 1)


if __name__ == '__main__':
    main()
