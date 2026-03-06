"""Deprecated: use research("read", ...) instead.
---
description: "[Deprecated] Fetch a URL — use read instead"
creates_nodes: [Source]
creates_edges: []
databases: [corcoran, lifestream]
---

This tool is a backward-compatibility wrapper around read_impl().
All new code should use research("read", ...) directly.

The read tool provides the same four-tier capture pipeline plus:
- stealth=true for anti-detection browsing (replaces browse_url)
- Unified parameter surface for reading + optional archiving
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lib.io import setup_output, load_params, output


def fetch_page_impl(url=None, **kwargs):
    """Deprecated: use research("read", ...) instead.

    Thin wrapper that delegates to read_impl() for backward compatibility.
    All parameters are passed through unchanged.
    """
    from tools.research.read import read_impl
    return read_impl(url=url, **kwargs)


# Subprocess entry point (backward compat)
if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = fetch_page_impl(**params)
    output(result)
