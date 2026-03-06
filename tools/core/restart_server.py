"""Restart the nicktools MCP server.

Triggers os._exit(0) after a brief delay, allowing the MCP response
to be sent back to the client first. The MCP client (Claude Desktop)
detects the process exit and restarts the server automatically.
---
description: Restart MCP server to pick up server.py changes
databases: []
---
"""
import os
import sys
import threading
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _delayed_exit(delay=0.5):
    """Exit after a delay to allow the response to be sent."""
    import time
    time.sleep(delay)
    os._exit(0)


def restart_server_impl(reason=None, driver=None, **kwargs):
    """Restart the MCP server process.

    Schedules a delayed os._exit(0) so the response can be returned first.
    The MCP client auto-restarts the server after exit.

    Args:
        reason: Optional reason for restart (logged in response)
    """
    # Stop the audit watcher gracefully before exit
    try:
        from lib.audit_watcher import stop_watcher
        stop_watcher()
    except Exception:
        pass  # Don't block restart on watcher issues

    # Schedule exit on a daemon thread so it doesn't block the response
    t = threading.Thread(target=_delayed_exit, args=(0.5,), daemon=True)
    t.start()

    msg = "Server restarting"
    if reason:
        msg += f": {reason}"
    msg += ". New process will pick up all server.py and TOOL_REGISTRY changes."

    return {"status": "restarting", "message": msg}


if __name__ == "__main__":
    # Subprocess fallback (shouldn't normally be used for restart)
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    result = restart_server_impl(**params)
    print(json.dumps(result, indent=2))
