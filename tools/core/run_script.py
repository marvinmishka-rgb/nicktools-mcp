"""Run a Python script on Windows with full stdout/stderr capture.
---
description: Run Python script with full stdout/stderr capture
databases: []
---
"""
import sys
import os
import subprocess
import tempfile
import traceback
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


def run_script_impl(script_path, args=None, timeout_seconds=60, driver=None, **kwargs):
    """Run a Python script with full stdout/stderr capture.

    Args:
        script_path: Path to .py file (absolute or relative to ClaudeFiles/scripts/)
        args: Optional space-separated arguments
        timeout_seconds: Max execution time (default 60, max 300)
        driver: Ignored (accepted for dispatch compatibility)

    Returns:
        str: Script output (stdout + stderr)
    """
    path = Path(script_path)
    if not path.is_absolute():
        path = SCRIPTS_DIR / path

    if not path.exists():
        return f"ERROR: Script not found: {path}"
    if not str(path).endswith('.py'):
        return f"ERROR: Only .py files allowed, got: {path}"

    cmd = [sys.executable, str(path)]
    if args:
        cmd.extend(args.split())

    timeout_seconds = min(timeout_seconds, 300)
    return _run_subprocess(cmd, timeout_seconds, str(SCRIPTS_DIR))


if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    result = run_script_impl(**params)
    # run_script returns a string directly, not a dict
    print(result)
