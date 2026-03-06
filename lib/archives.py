"""
Layer 2 -- Archive filesystem management + Wayback Machine config.

Depends on: lib.paths (Layer 0), lib.urls (Layer 1).
Absorbs archive_handler.py + archive/Wayback constants from config.py.

Directory structure:
    archives/
        {domain}/
            {date}_{hash}.html       - raw HTML capture
            {date}_{hash}.txt        - extracted text
            {date}_{hash}.meta.json  - capture metadata

Naming conventions:
    - domain: netloc with www. stripped, colons/slashes replaced with underscores
    - date: YYYY-MM-DD of capture
    - hash: first 12 chars of SHA-256 of the URL
"""
import os
import json
import hashlib
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

from lib.paths import ARCHIVES_DIR
from lib.urls import extract_domain


# -- Constants --

ARCHIVE_MIN_TEXT_SIZE = 200  # min chars for successful capture

# Wayback Machine / Internet Archive
# Optional: Set WAYBACK_S3_ACCESS and WAYBACK_S3_SECRET for Save Page Now API.
# Get credentials at https://archive.org/account/s3.php
# Without these, local archiving still works; only Wayback submission is disabled.
WAYBACK_S3_ACCESS = os.getenv("WAYBACK_S3_ACCESS", "")
WAYBACK_S3_SECRET = os.getenv("WAYBACK_S3_SECRET", "")
WAYBACK_CDX_URL = "http://archive.org/wayback/available"  # always reachable
WAYBACK_SPN_URL = "https://web.archive.org/save"  # reachable with VPN split tunnel (Feb 2026)


# -- Save Page Now (SPN) API --

def submit_to_spn(url, if_not_archived_within=None, capture_all=False):
    """Submit a URL to the Wayback Machine Save Page Now (SPN) API.

    Best-effort: failures are logged but never block local archiving.
    Runs synchronously via urllib (no async dependency).

    Args:
        url: The URL to submit for public archiving
        if_not_archived_within: Skip if archived within this period (e.g. '30d', '7d').
            Default: None (always submit).
        capture_all: If True, also capture embedded resources (images, CSS, JS).
            Default: False (page only -- faster, lower rate-limit cost).

    Returns:
        dict with keys:
            submitted (bool): Whether the request was sent successfully
            job_id (str|None): SPN job ID if submission accepted
            wayback_url (str|None): Final wayback URL if immediately available
            status (str): 'submitted', 'already_archived', 'rate_limited', 'error'
            detail (str): Human-readable explanation
    """
    if not WAYBACK_S3_ACCESS or not WAYBACK_S3_SECRET:
        return {
            "submitted": False,
            "job_id": None,
            "wayback_url": None,
            "status": "skipped",
            "detail": "WAYBACK_S3_ACCESS / WAYBACK_S3_SECRET not configured -- skipping SPN submission",
        }

    auth = f"LOW {WAYBACK_S3_ACCESS}:{WAYBACK_S3_SECRET}"

    # Build form data
    form_fields = {"url": url}
    if if_not_archived_within:
        form_fields["if_not_archived_within"] = if_not_archived_within
    if capture_all:
        form_fields["capture_all"] = "1"

    # URL-encode form body
    form_body = urllib.parse.urlencode(form_fields).encode("utf-8")

    req = urllib.request.Request(
        WAYBACK_SPN_URL,
        data=form_body,
        headers={
            "Authorization": auth,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "nicktools-archive/1.0",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

            job_id = data.get("job_id")
            wayback_url = data.get("url")

            # If url field has the /web/ prefix, it's the final wayback URL
            if wayback_url and "/web/" in wayback_url:
                final_url = wayback_url
            elif job_id:
                final_url = None  # Will need to poll status
            else:
                final_url = None

            return {
                "submitted": True,
                "job_id": job_id,
                "wayback_url": final_url,
                "status": "submitted",
                "detail": f"SPN accepted job {job_id}" if job_id else "SPN accepted",
                "raw": data,
            }

    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass

        if e.code == 429:
            return {
                "submitted": False,
                "job_id": None,
                "wayback_url": None,
                "status": "rate_limited",
                "detail": f"SPN rate limited (429). Body: {body[:200]}",
            }
        elif e.code == 520:
            # 520 often means "already captured recently"
            return {
                "submitted": False,
                "job_id": None,
                "wayback_url": None,
                "status": "already_archived",
                "detail": f"SPN returned 520 (likely already archived). Body: {body[:200]}",
            }
        else:
            return {
                "submitted": False,
                "job_id": None,
                "wayback_url": None,
                "status": "error",
                "detail": f"SPN HTTP {e.code}: {body[:200]}",
            }

    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {
            "submitted": False,
            "job_id": None,
            "wayback_url": None,
            "status": "error",
            "detail": f"SPN network error: {e}",
        }
    except Exception as e:
        return {
            "submitted": False,
            "job_id": None,
            "wayback_url": None,
            "status": "error",
            "detail": f"SPN unexpected error: {e}",
        }


def poll_spn_status(job_id, max_wait=60, poll_interval=5):
    """Poll SPN job status until completion or timeout.

    Args:
        job_id: The job_id from submit_to_spn()
        max_wait: Maximum seconds to wait (default: 60)
        poll_interval: Seconds between polls (default: 5)

    Returns:
        dict with keys:
            done (bool): Whether the job completed
            wayback_url (str|None): Final wayback URL if successful
            status (str): 'success', 'pending', 'error', 'timeout'
            detail (str): Human-readable explanation
    """
    if not WAYBACK_S3_ACCESS or not WAYBACK_S3_SECRET:
        return {
            "done": False,
            "wayback_url": None,
            "status": "skipped",
            "detail": "WAYBACK_S3_ACCESS / WAYBACK_S3_SECRET not configured",
        }

    auth = f"LOW {WAYBACK_S3_ACCESS}:{WAYBACK_S3_SECRET}"
    status_url = f"{WAYBACK_SPN_URL}/status/{job_id}"
    deadline = time.monotonic() + max_wait

    while time.monotonic() < deadline:
        req = urllib.request.Request(
            status_url,
            headers={
                "Authorization": auth,
                "User-Agent": "nicktools-archive/1.0",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            status = data.get("status")

            if status == "success":
                ts = data.get("timestamp", "")
                orig_url = data.get("original_url", "")
                wayback_url = f"https://web.archive.org/web/{ts}/{orig_url}" if ts else None
                return {
                    "done": True,
                    "wayback_url": wayback_url,
                    "status": "success",
                    "detail": f"Archived at {wayback_url}",
                    "timestamp": ts,
                }

            if status == "error":
                return {
                    "done": True,
                    "wayback_url": None,
                    "status": "error",
                    "detail": f"SPN job failed: {data.get('message', 'unknown')}",
                }

            # Still pending -- wait and retry
            time.sleep(poll_interval)

        except Exception as e:
            # Network glitch during poll -- wait and retry
            time.sleep(poll_interval)

    return {
        "done": False,
        "wayback_url": None,
        "status": "timeout",
        "detail": f"SPN job {job_id} did not complete within {max_wait}s",
    }


# -- Path Computation --

def archive_paths(url, archives_dir=None, date_str=None):
    """Compute filesystem paths for archiving a URL.

    Args:
        url: The URL being archived
        archives_dir: Override base directory (default: ARCHIVES_DIR)
        date_str: Override date string YYYY-MM-DD (default: today UTC)

    Returns:
        dict with keys:
            domain: bare domain string
            domain_dir: Path to domain directory
            base_name: filename stem (date_hash)
            html_path: Path for HTML file
            text_path: Path for text file
            meta_path: Path for metadata JSON
    """
    base = Path(archives_dir) if archives_dir else ARCHIVES_DIR
    domain = extract_domain(url)
    safe_domain = domain.replace(":", "_").replace("/", "_")
    domain_dir = base / safe_domain

    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:12]
    base_name = f"{date_str}_{url_hash}"

    return {
        "domain": domain,
        "domain_dir": domain_dir,
        "base_name": base_name,
        "html_path": domain_dir / f"{base_name}.html",
        "text_path": domain_dir / f"{base_name}.txt",
        "meta_path": domain_dir / f"{base_name}.meta.json",
    }


def ensure_domain_dir(paths):
    """Create the domain directory if it doesn't exist.

    Args:
        paths: dict from archive_paths()
    """
    paths["domain_dir"].mkdir(parents=True, exist_ok=True)


# -- Metadata --

def load_meta(meta_path):
    """Parse a .meta.json file.

    Args:
        meta_path: Path to the .meta.json file

    Returns:
        dict of metadata, or empty dict if file missing/invalid
    """
    meta_path = Path(meta_path)
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def validate_capture(text_path, min_size=None):
    """Check if an archive capture meets minimum quality.

    Args:
        text_path: Path to the .txt file
        min_size: Minimum character count (default: ARCHIVE_MIN_TEXT_SIZE)

    Returns:
        (ok: bool, reason: str) - reason is empty string if ok
    """
    min_size = min_size or ARCHIVE_MIN_TEXT_SIZE
    text_path = Path(text_path)

    if not text_path.exists():
        return False, "missing-text-file"

    try:
        text = text_path.read_text(encoding="utf-8")
    except OSError:
        return False, "unreadable-text-file"

    size = len(text.strip())
    if size == 0:
        return False, "empty-page"
    if size < min_size:
        return False, "insufficient-content"

    return True, ""


# -- Discovery --

def discover_archives(domain=None, archives_dir=None):
    """Find all archives on disk, optionally filtered by domain.

    Discovers archives by scanning for .meta.json files (the canonical
    indicator of a capture attempt). Returns metadata for each archive
    including file sizes and validation status.

    Args:
        domain: Filter to a specific domain (e.g. 'foxnews.com'). None = all.
        archives_dir: Override base directory (default: ARCHIVES_DIR)

    Returns:
        List of dicts, each with:
            domain, base_name, html_path, text_path, meta_path,
            meta (parsed dict), html_size, text_size,
            valid (bool), invalid_reason (str)
    """
    base = Path(archives_dir) if archives_dir else ARCHIVES_DIR
    results = []

    if domain:
        safe_domain = domain.replace(":", "_").replace("/", "_")
        search_dirs = [base / safe_domain]
    else:
        search_dirs = [d for d in base.iterdir() if d.is_dir()]

    for domain_dir in sorted(search_dirs):
        if not domain_dir.exists():
            continue
        dom_name = domain_dir.name

        # Find all meta files - each represents one capture
        for meta_path in sorted(domain_dir.glob("*.meta.json")):
            base_name = meta_path.stem.replace(".meta", "")
            html_path = domain_dir / f"{base_name}.html"
            text_path = domain_dir / f"{base_name}.txt"

            meta = load_meta(meta_path)
            valid, reason = validate_capture(text_path)

            results.append({
                "domain": dom_name,
                "base_name": base_name,
                "html_path": str(html_path),
                "text_path": str(text_path),
                "meta_path": str(meta_path),
                "meta": meta,
                "html_size": html_path.stat().st_size if html_path.exists() else 0,
                "text_size": text_path.stat().st_size if text_path.exists() else 0,
                "valid": valid,
                "invalid_reason": reason,
                "url": meta.get("url", ""),
            })

    return results


# -- Reconciliation --

def reconcile_with_graph(session, archives=None, archives_dir=None):
    """Compare filesystem archives against Source nodes in Neo4j.

    Identifies:
        - orphan_files: archives on disk with no matching Source node
        - ghost_sources: Source nodes with archiveStatus='captured' but no files
        - status_mismatches: Source.archiveStatus doesn't match filesystem reality

    Args:
        session: Active Neo4j session (lifestream database)
        archives: Pre-loaded archive list from discover_archives() (optional)
        archives_dir: Override base directory (default: ARCHIVES_DIR)

    Returns:
        dict with orphan_files, ghost_sources, status_mismatches, summary
    """
    if archives is None:
        archives = discover_archives(archives_dir=archives_dir)

    # Build a set of archived URLs from filesystem
    fs_urls = {}
    for arch in archives:
        url = arch.get("url", "")
        if url:
            fs_urls[url] = arch

    # Query all Source nodes from Neo4j
    result = session.run(
        "MATCH (s:Source) "
        "RETURN s.url AS url, s.domain AS domain, s.archiveStatus AS status, "
        "       s.title AS title, s.lastCaptured AS lastCaptured"
    )
    db_sources = {r["url"]: dict(r) for r in result}

    orphan_files = []
    ghost_sources = []
    status_mismatches = []

    # Files on disk with no Source node
    for url, arch in fs_urls.items():
        if url not in db_sources:
            orphan_files.append({
                "url": url,
                "domain": arch["domain"],
                "base_name": arch["base_name"],
            })

    # Source nodes claiming 'captured' but no files on disk
    for url, src in db_sources.items():
        if src["status"] == "captured" and url not in fs_urls:
            ghost_sources.append({
                "url": url,
                "domain": src["domain"],
                "title": src["title"],
            })

        # Status mismatch: source says 'captured' but file validation fails
        if url in fs_urls and src["status"] == "captured":
            arch = fs_urls[url]
            if not arch["valid"]:
                status_mismatches.append({
                    "url": url,
                    "domain": src["domain"],
                    "db_status": "captured",
                    "fs_status": arch["invalid_reason"],
                })

    return {
        "orphan_files": orphan_files,
        "ghost_sources": ghost_sources,
        "status_mismatches": status_mismatches,
        "summary": {
            "total_archives_on_disk": len(archives),
            "total_sources_in_db": len(db_sources),
            "orphan_count": len(orphan_files),
            "ghost_count": len(ghost_sources),
            "mismatch_count": len(status_mismatches),
        },
    }
