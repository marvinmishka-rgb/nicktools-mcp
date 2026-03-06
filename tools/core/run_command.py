"""Run a shell command with full stdout/stderr capture.
---
description: Run shell command (PowerShell/cmd) with output capture
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
from lib.paths import OUTPUT_DIR


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


def run_command_impl(command, shell="powershell", timeout_seconds=30, driver=None, **kwargs):
    """Run a shell command with full stdout/stderr capture.

    Args:
        command: The command to execute
        shell: 'powershell' or 'cmd' (default: powershell)
        timeout_seconds: Max execution time (default 30, max 120)
        driver: Ignored (accepted for dispatch compatibility)

    Returns:
        str: Command output (stdout + stderr)
    """
    timeout_seconds = min(timeout_seconds, 120)

    if shell == "powershell":
        cmd = ["powershell.exe", "-NoProfile", "-Command", command]
    else:
        cmd = ["cmd.exe", "/c", command]

    return _run_subprocess(cmd, timeout_seconds)


if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    result = run_command_impl(**params)
    print(result)
