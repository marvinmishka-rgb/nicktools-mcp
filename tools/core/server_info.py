"""Return server status, available tools, and environment info.
---
description: Return server status, tools, and environment info
databases: []
read_only: true
---
"""
import sys
import os
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.paths import SCRIPTS_DIR


def server_info_impl(driver=None, _server_context=None, **kwargs):
    """Return server status, available tools, and environment info.

    Args:
        driver: Shared Neo4j driver (to report status)
        _server_context: Dict of server-level state injected by dispatcher:
            server_name, server_version, tools_dir, in_process_tools

    Returns:
        dict: Server status information
    """
    ctx = _server_context or {}
    return {
        "server": ctx.get("server_name", "nicktools"),
        "version": ctx.get("server_version", "unknown"),
        "architecture": "hybrid dispatcher: in-process _impl() + subprocess fallback",
        "python": sys.version,
        "platform": sys.platform,
        "scripts_dir": str(SCRIPTS_DIR),
        "tools_dir": str(ctx.get("tools_dir", "")),
        "in_process_tools": ctx.get("in_process_tools", []),
        "shared_driver": "active" if driver is not None else "not initialized",
        "operation_count": ctx.get("operation_count", 0),
        "pid": os.getpid()
    }


if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    result = server_info_impl(**params)
    output(result)
