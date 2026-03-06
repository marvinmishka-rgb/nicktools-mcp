"""
Layer 0 -- Tool I/O harness.

No internal dependencies. Provides the standard subprocess tool interface:
setup_output() -> load_params() -> [do work] -> output(result)
"""
import sys
import io
import json


def setup_output():
    """Set up UTF-8 stdout. Call at top of every subprocess tool script."""
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def load_params():
    """Load params from the JSON file passed as sys.argv[1].

    Standard pattern: server writes params JSON, tool script reads it.
    """
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No params file provided"}))
        sys.exit(1)
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        return json.load(f)


def output(data, serializer=None):
    """Print JSON result to stdout (captured by server)."""
    print(json.dumps(data, indent=2, default=serializer or str))


# ---------------------------------------------------------------------------
# Key normalization -- camelCase -> snake_case at API return boundaries
# ---------------------------------------------------------------------------
import re

_CAMEL_RE1 = re.compile(r'([A-Z]+)([A-Z][a-z])')   # ABCDef -> ABC_Def
_CAMEL_RE2 = re.compile(r'([a-z0-9])([A-Z])')        # abcDef -> abc_Def


def camel_to_snake(name: str) -> str:
    """Convert a camelCase string to snake_case.

    Handles consecutive capitals correctly:
        sessionId     -> session_id
        auditSizeKB   -> audit_size_kb
        firstAuditTS  -> first_audit_ts
        topTools      -> top_tools
        error         -> error  (unchanged)
    """
    s = _CAMEL_RE1.sub(r'\1_\2', name)
    return _CAMEL_RE2.sub(r'\1_\2', s).lower()


def normalize_keys(obj):
    """Recursively normalize dict keys from camelCase to snake_case.

    Use at API return boundaries to decouple the public interface from
    internal naming conventions (Neo4j properties, Cowork audit metadata).
    Passes through non-dict/list values unchanged.
    """
    if isinstance(obj, dict):
        return {camel_to_snake(k): normalize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize_keys(item) for item in obj]
    return obj
