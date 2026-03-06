"""
wayback_lookup -- Query the Wayback Machine CDX API for existing snapshots.

Returns snapshot availability, timestamps, and URLs for any given URL.
Uses archive.org/wayback/available (always reachable from Python) and
the full CDX server at web.archive.org/cdx/ (may be blocked -- gracefully
falls back to the availability API).

Designed as an in-process tool (_impl function) for fast dispatch.
Also works standalone via subprocess with params JSON.

S3 credentials for SPN integration are loaded from environment variables
WAYBACK_S3_ACCESS and WAYBACK_S3_SECRET. Get yours at https://archive.org/account/s3.php
---
description: Query Wayback Machine CDX API for URL snapshots
databases: []
read_only: true
---
"""
import json
import urllib.request
import urllib.error
import urllib.parse
import socket
from datetime import datetime

# Add nicktools package to path for lib/ modules
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def wayback_lookup_impl(url, count=5, from_date=None, to_date=None, **kwargs):
    """Query Wayback Machine for snapshots of a URL.

    Args:
        url: The URL to look up
        count: Max snapshots to return (default 5, max 25)
        from_date: Optional start date filter (YYYYMMDD or YYYY)
        to_date: Optional end date filter (YYYYMMDD or YYYY)
        **kwargs: Absorbs driver= and other injected params

    Returns:
        dict with snapshot info, CDX results, and connectivity status
    """
    count = min(max(1, count), 25)
    result = {
        "url": url,
        "queried_at": datetime.now().isoformat(),
        "snapshots": [],
        "total_found": 0,
    }

    # -- Step 1: Availability API (always works via archive.org) --
    avail_url = f"http://archive.org/wayback/available?url={urllib.parse.quote(url, safe=':/')}"
    try:
        req = urllib.request.Request(avail_url, headers={"User-Agent": "nicktools/1.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        avail_data = json.loads(resp.read().decode())
        closest = avail_data.get("archived_snapshots", {}).get("closest")
        if closest:
            result["latest_snapshot"] = {
                "url": closest.get("url", ""),
                "timestamp": closest.get("timestamp", ""),
                "status": closest.get("status", ""),
                "available": closest.get("available", False),
            }
            # Parse timestamp to human-readable
            ts = closest.get("timestamp", "")
            if len(ts) >= 8:
                result["latest_snapshot"]["date"] = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        else:
            result["latest_snapshot"] = None
            result["message"] = "No snapshots found in Wayback Machine"
            return result
    except Exception as e:
        result["availability_error"] = str(e)
        # Don't return yet -- try CDX

    # -- Step 2: CDX API for full history (may be blocked) --
    # CDX server is on web.archive.org which may not be reachable
    cdx_reachable = False
    try:
        sock = socket.create_connection(("web.archive.org", 80), timeout=3)
        sock.close()
        cdx_reachable = True
    except:
        pass

    if cdx_reachable:
        # Build CDX query
        cdx_params = {
            "url": url,
            "output": "json",
            "limit": str(count),
            "fl": "timestamp,statuscode,digest,length,mimetype",
            "sort": "reverse",  # newest first
        }
        if from_date:
            cdx_params["from"] = from_date
        if to_date:
            cdx_params["to"] = to_date

        cdx_url = f"http://web.archive.org/cdx/search/cdx?{urllib.parse.urlencode(cdx_params)}"

        try:
            req = urllib.request.Request(cdx_url, headers={"User-Agent": "nicktools/1.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            cdx_data = json.loads(resp.read().decode())

            if len(cdx_data) > 1:  # First row is header
                headers = cdx_data[0]
                for row in cdx_data[1:]:
                    entry = dict(zip(headers, row))
                    ts = entry.get("timestamp", "")
                    snapshot = {
                        "timestamp": ts,
                        "date": f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}" if len(ts) >= 8 else ts,
                        "status_code": entry.get("statuscode", ""),
                        "size": entry.get("length", ""),
                        "mimetype": entry.get("mimetype", ""),
                        "wayback_url": f"https://web.archive.org/web/{ts}/{url}",
                    }
                    result["snapshots"].append(snapshot)

                result["total_found"] = len(cdx_data) - 1
                result["cdx_source"] = "web.archive.org (direct)"
        except Exception as e:
            result["cdx_error"] = str(e)
    else:
        result["cdx_source"] = "unavailable (web.archive.org blocked)"
        # Build snapshot URL from availability API data if we have it
        if result.get("latest_snapshot") and result["latest_snapshot"].get("url"):
            ts = result["latest_snapshot"]["timestamp"]
            result["snapshots"] = [{
                "timestamp": ts,
                "date": result["latest_snapshot"].get("date", ""),
                "wayback_url": result["latest_snapshot"]["url"],
                "status_code": result["latest_snapshot"].get("status", ""),
                "note": "From availability API only (CDX blocked)"
            }]
            result["total_found"] = 1

    # -- Step 3: Connectivity summary --
    result["connectivity"] = {
        "archive_org": True,  # Always true if we got here
        "web_archive_org": cdx_reachable,
        "note": "web.archive.org blocked from Python -- user browser can access" if not cdx_reachable else "full CDX access available"
    }

    return result


# -- Subprocess entry point --
if __name__ == "__main__":
    import urllib.parse
    from lib.io import load_params, output
    p = load_params()
    result = wayback_lookup_impl(**p)
    output(result)
