"""Graph backup: export full database as Cypher statements or JSON.
---
description: Export Neo4j database using APOC export for disaster recovery
creates_nodes: []
creates_edges: []
databases: [corcoran, lifestream]
---

Uses apoc.export.cypher.all() or apoc.export.json.all() to dump the full
database. Saves to ClaudeFiles/backups/ with timestamped filenames.
"""
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent.parent))
from lib.io import setup_output, load_params, output
from lib.db import execute_write, execute_read, GRAPH_DATABASE


from lib.paths import CLAUDE_FILES, ensure_dir
BACKUP_DIR = CLAUDE_FILES / "backups"


def backup_graph_impl(database=GRAPH_DATABASE, format="cypher", driver=None, **kwargs):
    """Export full database for backup.

    Args:
        database: Neo4j database to back up (default: corcoran)
        format: Export format -- "cypher" or "json" (default: cypher)
        driver: Shared Neo4j driver

    Returns:
        dict with file path, size, and entity counts
    """
    ensure_dir(BACKUP_DIR, "graph backup directory")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    if format == "cypher":
        ext = "cypher"
        # Use apoc.export.cypher.all with stream mode (returns data, doesn't write to file)
        export_cypher = """
        CALL apoc.export.cypher.all(null, {
            format: 'cypher-shell',
            useOptimizations: {type: 'UNWIND_BATCH', unwindBatchSize: 20},
            stream: true
        })
        YIELD cypherStatements
        RETURN cypherStatements
        """
    elif format == "json":
        ext = "json"
        export_cypher = """
        CALL apoc.export.json.all(null, {stream: true})
        YIELD data
        RETURN data
        """
    else:
        return {"error": f"Unknown format '{format}'. Valid: cypher, json"}

    filename = f"{database}_{timestamp}.{ext}"
    filepath = BACKUP_DIR / filename

    try:
        # Get entity counts first
        count_records, _ = execute_read(
            "MATCH (n) RETURN count(n) AS nodes "
            "UNION ALL "
            "MATCH ()-[r]->() RETURN count(r) AS nodes",
            database=database, driver=driver
        )
        node_count = count_records[0]["nodes"] if count_records else 0
        rel_count = count_records[1]["nodes"] if len(count_records) > 1 else 0

        # Run export
        records, _ = execute_read(
            export_cypher, database=database, driver=driver
        )

        if not records:
            return {"error": "Export returned no data. Is APOC installed?"}

        if format == "cypher":
            content = records[0].get("cypherStatements", "")
        else:
            # JSON export may return multiple records
            content = "\n".join(r.get("data", "") for r in records)

        if not content:
            return {"error": "Export produced empty content"}

        filepath.write_text(content, encoding='utf-8')
        file_size = filepath.stat().st_size

        return {
            "status": "success",
            "database": database,
            "format": format,
            "file": str(filepath),
            "filename": filename,
            "file_size": file_size,
            "file_size_human": f"{file_size / 1024:.1f} KB" if file_size < 1048576 else f"{file_size / 1048576:.1f} MB",
            "nodes": node_count,
            "relationships": rel_count,
            "timestamp": timestamp,
        }

    except Exception as e:
        import traceback
        return {"error": f"Backup failed: {e}", "traceback": traceback.format_exc()}


# ============================================================
# Subprocess entry point (fallback -- primary path is in-process)
# ============================================================

if __name__ == "__main__":
    setup_output()
    params = load_params()
    result = backup_graph_impl(**params)
    output(result)
