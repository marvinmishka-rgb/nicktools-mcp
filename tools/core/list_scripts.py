"""List available Python scripts that can be run via run_script.
---
description: List available Python scripts under ClaudeFiles/scripts/
databases: []
read_only: true
---
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from lib.paths import SCRIPTS_DIR


def list_scripts_impl(directory=None, driver=None, **kwargs):
    """List available Python scripts under ClaudeFiles/scripts/.

    Args:
        directory: Optional subdirectory to list
        driver: Ignored (accepted for dispatch compatibility)

    Returns:
        str: Formatted list of scripts with sizes
    """
    target = SCRIPTS_DIR
    if directory:
        target = SCRIPTS_DIR / directory

    if not target.exists():
        return f"ERROR: Directory not found: {target}"

    scripts = []
    for f in sorted(target.rglob("*.py")):
        rel = f.relative_to(SCRIPTS_DIR)
        size = f.stat().st_size
        scripts.append(f"  {rel} ({size:,} bytes)")

    return f"Scripts in {target}:\n" + "\n".join(scripts) if scripts else "No .py files found"


if __name__ == "__main__":
    from lib.io import setup_output, load_params, output
    setup_output()
    params = load_params()
    result = list_scripts_impl(**params)
    print(result)
