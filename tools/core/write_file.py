"""Write content to a file on the Windows filesystem.
---
description: Write content to file on Windows filesystem
databases: []
---
"""
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.paths import ensure_dir


def write_file_impl(path, content, encoding="utf-8", driver=None, **kwargs):
    """Write content to a file, creating parent directories as needed.

    Args:
        path: Absolute path to the file
        content: Text content to write
        encoding: File encoding (default: utf-8)
        driver: Ignored (accepted for dispatch compatibility)

    Returns:
        str: Success message or error
    """
    try:
        p = Path(path)
        ensure_dir(p.parent, "parent directory for output file")
        p.write_text(content, encoding=encoding)
        return f"Written {len(content):,} chars to {path}"
    except Exception as e:
        return f"ERROR: {e}"


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    result = write_file_impl(**params)
    print(result)
