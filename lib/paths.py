"""
Layer 0 -- Filesystem constants.

No internal dependencies. Every path used by nicktools is defined here.

Configuration:
    Set NICKTOOLS_HOME env var to your workspace root.
    All other paths derive from it.

    Resolution order:
    1. NICKTOOLS_HOME env var (if set)
    2. ~/nicktools_workspace (if it exists or ClaudeFiles/ can be created there)
    3. User home directory (legacy fallback)
"""
import os
from pathlib import Path


def _resolve_home():
    """Resolve the workspace root directory."""
    # Explicit env var takes priority
    env_home = os.getenv("NICKTOOLS_HOME")
    if env_home:
        return Path(env_home)

    # Check if ~/nicktools_workspace exists (new installs)
    default = Path.home() / "nicktools_workspace"
    if default.exists():
        return default

    # Check if ~/ClaudeFiles exists (legacy/development layout)
    legacy = Path.home()
    if (legacy / "ClaudeFiles").exists():
        return legacy

    # New install -- use default
    return default


USER_HOME = _resolve_home()
CLAUDE_FILES = USER_HOME / "ClaudeFiles"
SCRIPTS_DIR = CLAUDE_FILES / "scripts"
LIFESTREAM_DIR = CLAUDE_FILES / "lifestream" / "stream"
ARCHIVES_DIR = CLAUDE_FILES / "archives"
OUTPUT_DIR = CLAUDE_FILES

# Browse infrastructure paths
BROWSE_STATE_DIR = CLAUDE_FILES / ".browse_state"

# Async archive pipeline
ARCHIVE_QUEUE_DIR = CLAUDE_FILES / "archive_queue"
ARCHIVE_QUEUE_FILE = ARCHIVE_QUEUE_DIR / "queue.json"
ARCHIVE_STAGED_DIR = ARCHIVE_QUEUE_DIR / "staged"

# Manual capture inbox -- user saves pages here for ingestion
SAVED_PAGES_DIR = CLAUDE_FILES / "uploads" / "websites"


def ensure_dir(path, purpose=""):
    """Create directory if it doesn't exist. Helpful error on failure.

    Args:
        path: Directory path to ensure exists
        purpose: Human-readable description for error messages (e.g., "archive storage")

    Returns:
        Path object for the directory
    """
    path = Path(path)
    if not path.exists():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            purpose_str = f" ({purpose})" if purpose else ""
            raise OSError(
                f"Cannot create directory{purpose_str}: {path}\n"
                f"Original error: {e}\n"
                f"Check that NICKTOOLS_HOME is set to a writable location."
            ) from e
    return path
