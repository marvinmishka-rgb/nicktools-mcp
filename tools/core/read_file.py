"""Read a file from the Windows filesystem.
---
description: Read text file from Windows filesystem
databases: []
read_only: true
---
"""
from pathlib import Path


SOFT_LIMIT = 200_000   # truncate with signal above 200KB
HARD_LIMIT = 2_000_000  # reject above 2MB


def read_file_impl(path, encoding="utf-8", driver=None, **kwargs):
    """Read a file and return its contents.

    Args:
        path: Absolute path to the file
        encoding: File encoding (default: utf-8)
        driver: Ignored (accepted for dispatch compatibility)

    Returns:
        str: File contents (truncated at 200KB with signal), or error message.
             Files above 2MB are rejected outright.
    """
    try:
        p = Path(path)
        if not p.exists():
            return f"ERROR: File not found: {path}"
        size = p.stat().st_size
        if size > HARD_LIMIT:
            return f"ERROR: File too large ({size:,} bytes, {size/1_048_576:.1f} MB). Max 2MB."
        text = p.read_text(encoding=encoding, errors='replace')
        if len(text) > SOFT_LIMIT:
            return text[:SOFT_LIMIT] + f"\n\n[TRUNCATED at {SOFT_LIMIT:,} chars — {len(text):,} total]"
        return text
    except Exception as e:
        return f"ERROR: {e}"


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    result = read_file_impl(**params)
    print(result)
