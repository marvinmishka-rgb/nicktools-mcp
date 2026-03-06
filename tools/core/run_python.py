"""Execute inline Python code with full stdout/stderr capture.
---
description: Execute inline Python code with output capture and optional Neo4j query injection
databases: [*]
---

Supports a `queries` parameter for injecting Neo4j results into the script
without the data ever touching the context window. The server runs each query
via the shared driver, writes results to temp JSON files, and makes them
available to the script via a `_query_results` dict mapping names to file paths.

Example:
    core("run_python", {
        "code": "import json\\nwith open(_query_results['agents']) as f:\\n    agents = json.load(f)\\nprint(len(agents))",
        "queries": {
            "agents": {"cypher": "MATCH (a:Agent) RETURN a.name AS name", "database": "corcoran"}
        }
    })
"""
import sys
import os
import subprocess
import tempfile
import traceback
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.paths import SCRIPTS_DIR, OUTPUT_DIR


def _run_subprocess(cmd, timeout, cwd=None):
    """Sync subprocess runner with temp-file stdout/stderr capture."""
    stdout_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='_out.txt', delete=False, dir=str(OUTPUT_DIR))
    stderr_file = tempfile.NamedTemporaryFile(
        mode='w', suffix='_err.txt', delete=False, dir=str(OUTPUT_DIR))
    try:
        proc = subprocess.run(
            cmd, stdin=subprocess.DEVNULL,
            stdout=stdout_file, stderr=stderr_file,
            timeout=timeout, cwd=cwd,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        )
        stdout_file.close()
        stderr_file.close()
        stdout_text = Path(stdout_file.name).read_text(encoding='utf-8', errors='replace')
        stderr_text = Path(stderr_file.name).read_text(encoding='utf-8', errors='replace')
        result_parts = []
        if stdout_text.strip():
            result_parts.append(stdout_text)
        if stderr_text.strip():
            result_parts.append(f"[STDERR]\n{stderr_text}")
        if proc.returncode != 0:
            result_parts.append(f"[EXIT CODE: {proc.returncode}]")
        return "\n".join(result_parts) if result_parts else "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: Timed out after {timeout}s"
    except Exception:
        return f"ERROR: {traceback.format_exc()}"
    finally:
        for f in (stdout_file, stderr_file):
            try:
                os.unlink(f.name)
            except Exception:
                pass


def _execute_queries(queries, driver):
    """Run pre-queries via shared driver, write results to temp JSON files.

    Args:
        queries: dict mapping names to {cypher, database?, params?} dicts
        driver: Shared Neo4j driver

    Returns:
        dict mapping names to temp file paths containing JSON results
    """
    from lib.db import execute_read, execute_write, check_query_type, GRAPH_DATABASE

    result_files = {}
    for name, spec in queries.items():
        cypher = spec.get("cypher", "")
        database = spec.get("database", GRAPH_DATABASE)
        params = spec.get("params", {})

        if not cypher:
            continue

        try:
            # Use read or write based on query type
            is_write = check_query_type(cypher) == "write"
            if is_write:
                records, _ = execute_write(cypher, database=database, driver=driver, **params)
            else:
                records, _ = execute_read(cypher, database=database, driver=driver, **params)

            # Convert records to serializable dicts
            data = [dict(r) for r in records]

            # Write to temp file
            tf = tempfile.NamedTemporaryFile(
                mode='w', suffix=f'_{name}.json', delete=False,
                dir=str(OUTPUT_DIR), encoding='utf-8'
            )
            json.dump(data, tf, default=str)
            tf.close()
            result_files[name] = tf.name

        except Exception as e:
            # Write error to file so script can handle it
            tf = tempfile.NamedTemporaryFile(
                mode='w', suffix=f'_{name}_error.json', delete=False,
                dir=str(OUTPUT_DIR), encoding='utf-8'
            )
            json.dump({"_error": str(e)}, tf)
            tf.close()
            result_files[name] = tf.name

    return result_files


def run_python_impl(code, timeout_seconds=60, queries=None, driver=None, **kwargs):
    """Execute inline Python code with full stdout/stderr capture.

    Writes code to a temp file and runs it -- no quoting issues.

    Args:
        code: Python source code to execute
        timeout_seconds: Max execution time (default 60, max 300)
        queries: Optional dict mapping names to {cypher, database} dicts.
            Each query is executed via the shared Neo4j driver BEFORE the
            script runs. Results are written to temp JSON files and made
            available via a `_query_results` dict injected into the script.
            Example: {"agents": {"cypher": "MATCH (a:Agent) RETURN a.name AS name"}}
            In the script: json.load(open(_query_results['agents']))
        driver: Shared Neo4j driver (used for queries parameter)

    Returns:
        str: Script output (stdout + stderr)
    """
    timeout_seconds = min(timeout_seconds, 300)

    # Execute pre-queries if provided
    query_files = {}
    if queries and isinstance(queries, dict) and driver:
        query_files = _execute_queries(queries, driver)

    # Build the preamble that injects query result file paths
    preamble_lines = []
    if query_files:
        # Inject _query_results dict with file paths
        preamble_lines.append("# -- injected by run_python query system --")
        paths_dict = {name: path.replace("\\", "\\\\") for name, path in query_files.items()}
        preamble_lines.append(f"_query_results = {json.dumps(paths_dict)}")
        preamble_lines.append("# -- end injection --\n")

    full_code = "\n".join(preamble_lines) + code if preamble_lines else code

    script = tempfile.NamedTemporaryFile(
        mode='w', suffix='.py', delete=False,
        dir=str(OUTPUT_DIR), encoding='utf-8'
    )
    script.write(full_code)
    script.close()

    try:
        return _run_subprocess(
            [sys.executable, script.name], timeout_seconds, str(SCRIPTS_DIR)
        )
    finally:
        # Clean up script file
        try:
            os.unlink(script.name)
        except Exception:
            pass
        # Clean up query result files
        for path in query_files.values():
            try:
                os.unlink(path)
            except Exception:
                pass


if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    result = run_python_impl(**params)
    print(result)
