#!/usr/bin/env python3
"""Query the live audit watcher status.

Returns current state, counters, and signal summaries from the background
audit watcher thread.
---
description: Live audit watcher status and signals
databases: []
---
"""


def watcher_status_impl(**kwargs):
    """Return the live audit watcher status.

    No parameters needed -- returns current state.
    """
    from lib.audit_watcher import get_watcher_status
    return get_watcher_status()
