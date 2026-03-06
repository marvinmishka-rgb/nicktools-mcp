"""
Layer 1 -- Entry paths, ID generation, entry type validation.

Depends on: lib.paths (Layer 0).
"""
from datetime import datetime
from lib.paths import USER_HOME, LIFESTREAM_DIR


# -- Constants --

VALID_ENTRY_TYPES = {
    "idea", "finding", "decision", "question", "connection",
    "artifact", "session", "milestone", "analysis", "reflection",
    "draft", "session-narrative"
}


# -- Functions --

def normalize_path(path):
    """Convert a Windows absolute path to a relative path from user home.

    Used for storing paths in Neo4j that are portable and readable.
    Example: 'C:\\Users\\you\\ClaudeFiles\\foo.md' -> 'ClaudeFiles/foo.md'
    """
    return str(path).replace(str(USER_HOME) + "\\", "").replace("\\", "/")


def entry_path(entry_id, base_dir=None):
    """Resolve a lifestream entry ID to its .md file path.

    Args:
        entry_id: e.g. 'ls-20260224-015'
        base_dir: Override for LIFESTREAM_DIR (default: LIFESTREAM_DIR)

    Returns:
        Path to the .md file, e.g. LIFESTREAM_DIR/2026/02/24/ls-20260224-015.md
    """
    base = base_dir or LIFESTREAM_DIR
    parts = entry_id.split("-")  # ['ls', '20260224', '015']
    date_part = parts[1]
    return base / date_part[:4] / date_part[4:6] / date_part[6:8] / f"{entry_id}.md"


def next_entry_id(session, date=None):
    """Generate the next sequential entry ID for a given date.

    Args:
        session: Active Neo4j session (lifestream database)
        date: Date string YYYYMMDD (default: today)

    Returns:
        Tuple of (entry_id, session_date) e.g. ('ls-20260225-001', '2026-02-25')
    """
    today = date or datetime.now().strftime("%Y%m%d")
    prefix = f"ls-{today}-"

    result = session.run(
        "MATCH (s:StreamEntry) WHERE s.id STARTS WITH $prefix "
        "RETURN s.id ORDER BY s.id DESC LIMIT 1",
        {"prefix": prefix}
    )
    records = list(result)
    if records:
        last_num = int(records[0]["s.id"].split("-")[-1])
        next_num = last_num + 1
    else:
        next_num = 1

    entry_id = f"ls-{today}-{next_num:03d}"
    session_date = f"{today[:4]}-{today[4:6]}-{today[6:8]}"
    return entry_id, session_date
