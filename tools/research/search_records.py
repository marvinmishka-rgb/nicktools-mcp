"""
search_records -- Query free public record APIs for entity information.

Searches multiple public record databases by name. Returns structured results
from each source that's available. APIs requiring keys are skipped gracefully.

The SOURCE_REGISTRY at the top of this file defines all available sources --
their descriptions, env var gates, and whether they're included by default.
To add a new source: add a registry entry, write a search function, and add
a dispatch case in _dispatch_source().

Designed as an in-process tool (_impl function) for fast dispatch.
Also works standalone via subprocess with params JSON.
---
description: Search public records (SEC, patents, OpenCorporates, courts, FMCSA, NHTSA)
databases: []
read_only: true
---
"""
import json
import os
import urllib.request
import urllib.error
import urllib.parse
import socket
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# API keys -- set these in .env or environment to enable additional sources
OPENCORPORATES_API_TOKEN = os.getenv("OPENCORPORATES_API_TOKEN", "")
COURTLISTENER_API_TOKEN = os.getenv("COURTLISTENER_API_TOKEN", "")
PLATETOVIN_API_KEY = os.getenv("PLATETOVIN_API_KEY", "")
FMCSA_WEB_KEY = os.getenv("FMCSA_WEB_KEY", "")
SEC_USER_AGENT = os.getenv("SEC_USER_AGENT", "")
_SEC_CONFIGURED = bool(SEC_USER_AGENT and "your-email" not in SEC_USER_AGENT)

# Request timeout in seconds
REQUEST_TIMEOUT = 15


# -- Source Registry ----------------------------------------------------------
# Each entry defines a searchable public record source. Fields:
#   key:            Identifier used in record_types parameter and result keys
#   result_key:     Key under which results appear in the response dict
#   description:    Human-readable description (shown in help/docs)
#   env_var:        Environment variable that gates this source (None = always available)
#   env_url:        Registration URL for the env var (shown when skipped)
#   default_on:     Whether included in the default "all" source set
#   auto_include:   Query pattern that auto-includes this source ("vin", "dot", or None)
#
# The search functions themselves are defined below. To add a new source:
#   1. Add an entry to SOURCE_REGISTRY
#   2. Write a _search_* or _get_* function
#   3. Add a dispatch case in _dispatch_source()

SOURCE_REGISTRY = {
    "sec": {
        "result_key": "sec_edgar",
        "description": "SEC EDGAR full-text search (free, requires User-Agent)",
        "env_var": "SEC_USER_AGENT",
        "env_url": "https://www.sec.gov/os/accessing-edgar-data -- set SEC_USER_AGENT to your contact email",
        "default_on": True,
        "auto_include": None,
    },
    "sec_company": {
        "result_key": "sec_company",
        "description": "SEC EDGAR company name/CIK lookup (free, requires User-Agent)",
        "env_var": "SEC_USER_AGENT",
        "env_url": "https://www.sec.gov/os/accessing-edgar-data -- set SEC_USER_AGENT to your contact email",
        "default_on": True,
        "auto_include": None,
    },
    "patents": {
        "result_key": "patents",
        "description": "Google Patents search by inventor, assignee, or keywords (free, no auth)",
        "env_var": None,
        "env_url": None,
        "default_on": True,
        "auto_include": None,
    },
    "opencorporates": {
        "result_key": "opencorporates",
        "description": "Global company registry (optional API key, 50 requests/day free)",
        "env_var": "OPENCORPORATES_API_TOKEN",
        "env_url": "https://opencorporates.com/users/sign_up",
        "default_on": True,
        "auto_include": None,
    },
    "courtlistener": {
        "result_key": "courtlistener",
        "description": "Federal/state court records (optional API key, 5K requests/day free)",
        "env_var": "COURTLISTENER_API_TOKEN",
        "env_url": "https://www.courtlistener.com/sign-in/",
        "default_on": True,
        "auto_include": None,
    },
    "nhtsa": {
        "result_key": "nhtsa_vin",
        "description": "NHTSA VIN decode -- make, model, year, engine (free, no auth)",
        "env_var": None,
        "env_url": None,
        "default_on": False,
        "auto_include": "vin",
    },
    "fmcsa": {
        "result_key": "fmcsa",
        "description": "FMCSA motor carrier safety data (requires web key)",
        "env_var": "FMCSA_WEB_KEY",
        "env_url": "https://mobile.fmcsa.dot.gov/QCDevsite/",
        "default_on": False,
        "auto_include": "dot",
    },
}


def _http_get_json(url, headers=None, timeout=REQUEST_TIMEOUT):
    """Make an HTTP GET request and parse JSON response."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            return json.loads(data)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except:
            pass
        return {"_error": f"HTTP {e.code}: {e.reason}", "_body": body}
    except urllib.error.URLError as e:
        return {"_error": f"URL error: {e.reason}"}
    except socket.timeout:
        return {"_error": f"Timeout after {timeout}s"}
    except json.JSONDecodeError as e:
        return {"_error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"_error": str(e)}


def _is_vin(s):
    """Check if a string looks like a VIN (17 alphanumeric chars, no I/O/Q)."""
    if not s or len(s) != 17:
        return False
    return all(c.isalnum() and c.upper() not in ('I', 'O', 'Q') for c in s)


def _decode_nhtsa_vin(vin, model_year=None):
    """Decode VIN using NHTSA vPIC API (free, no auth, no rate limit).

    Endpoint: https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{VIN}?format=json
    Optional: &modelyear={year} for better accuracy on ambiguous VINs.

    Args:
        vin: 17-character Vehicle Identification Number
        model_year: Optional model year for better decode accuracy

    Returns:
        dict with decoded vehicle data or error
    """
    vin = vin.strip().upper()
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVin/{vin}?format=json"
    if model_year:
        url += f"&modelyear={model_year}"

    result = _http_get_json(url)
    if "_error" in result:
        return {"source": "nhtsa_vin", "error": result["_error"], "vin": vin}

    # Build lookup from Results array (Variable -> Value)
    raw = {}
    errors = []
    for r in result.get("Results", []):
        var_name = r.get("Variable", "")
        val = (r.get("Value") or "").strip()
        if val:
            raw[var_name] = val
        # Error code field (Variable ID 143)
        if var_name == "Error Code" and val and val != "0":
            errors.append({"code": val, "detail": r.get("ValueId", "")})

    # Extract key fields
    decoded = {
        "source": "nhtsa_vin",
        "vin": vin,
        "make": raw.get("Make", ""),
        "model": raw.get("Model", ""),
        "year": raw.get("Model Year", ""),
        "body_class": raw.get("Body Class", ""),
        "vehicle_type": raw.get("Vehicle Type", ""),
        "drive_type": raw.get("Drive Type", ""),
        "trim": raw.get("Trim", ""),
        "series": raw.get("Series", ""),
        "doors": raw.get("Doors", ""),
        "engine_displacement": raw.get("Displacement (L)", ""),
        "engine_cylinders": raw.get("Engine Number of Cylinders", ""),
        "fuel_type": raw.get("Fuel Type - Primary", ""),
        "transmission": raw.get("Transmission Style", ""),
        "plant_city": raw.get("Plant City", ""),
        "plant_state": raw.get("Plant State", ""),
        "plant_country": raw.get("Plant Country", ""),
        "manufacturer": raw.get("Manufacturer Name", ""),
        "gvwr": raw.get("Gross Vehicle Weight Rating From", ""),
        "error_codes": errors,
        "raw_field_count": len(raw),
    }

    # Build a human-readable engine string
    engine_parts = []
    if decoded["engine_displacement"]:
        engine_parts.append(f"{decoded['engine_displacement']}L")
    if decoded["engine_cylinders"]:
        engine_parts.append(f"{decoded['engine_cylinders']}cyl")
    if decoded["fuel_type"]:
        engine_parts.append(decoded["fuel_type"])
    decoded["engine_summary"] = " ".join(engine_parts)

    return decoded


def _get_nhtsa_recalls(make, model, model_year):
    """Get safety recalls for a vehicle from NHTSA (free, no auth).

    Endpoint: https://api.nhtsa.gov/recalls/recallsByVehicle

    Args:
        make: Vehicle make (e.g. "Ford")
        model: Vehicle model (e.g. "Edge")
        model_year: Model year (e.g. "2008")

    Returns:
        dict with recall records
    """
    params = urllib.parse.urlencode({
        "make": make, "model": model, "modelYear": model_year
    })
    url = f"https://api.nhtsa.gov/recalls/recallsByVehicle?{params}"
    result = _http_get_json(url)
    if "_error" in result:
        return {"source": "nhtsa_recalls", "error": result["_error"]}

    records = []
    for r in result.get("results", []):
        records.append({
            "nhtsa_campaign": r.get("NHTSACampaignNumber", ""),
            "component": r.get("Component", ""),
            "summary": (r.get("Summary", "") or "")[:300],
            "consequence": (r.get("Consequence", "") or "")[:200],
            "remedy": (r.get("Remedy", "") or "")[:200],
            "report_date": r.get("ReportReceivedDate", ""),
            "manufacturer": r.get("Manufacturer", ""),
        })

    return {
        "source": "nhtsa_recalls",
        "make": make, "model": model, "year": model_year,
        "total": result.get("Count", len(records)),
        "records": records,
    }


def _get_nhtsa_complaints(make, model, model_year):
    """Get owner complaints for a vehicle from NHTSA (free, no auth).

    Endpoint: https://api.nhtsa.gov/complaints/complaintsByVehicle

    Args:
        make: Vehicle make
        model: Vehicle model
        model_year: Model year

    Returns:
        dict with complaint records
    """
    params = urllib.parse.urlencode({
        "make": make, "model": model, "modelYear": model_year
    })
    url = f"https://api.nhtsa.gov/complaints/complaintsByVehicle?{params}"
    result = _http_get_json(url)
    if "_error" in result:
        return {"source": "nhtsa_complaints", "error": result["_error"]}

    records = []
    for r in result.get("results", []):
        records.append({
            "odi_number": r.get("odiNumber", ""),
            "component": r.get("components", ""),
            "summary": (r.get("summary", "") or "")[:300],
            "crash": r.get("crash", False),
            "fire": r.get("fire", False),
            "injuries": r.get("numberOfInjuries", 0),
            "date_complaint": r.get("dateComplaintFiled", ""),
            "date_of_incident": r.get("dateOfIncident", ""),
        })

    return {
        "source": "nhtsa_complaints",
        "make": make, "model": model, "year": model_year,
        "total": result.get("Count", len(records)),
        "records": records,
    }


def _is_dot_number(s):
    """Check if a string looks like an FMCSA DOT number (1-8 digits)."""
    if not s:
        return False
    s = s.strip()
    return s.isdigit() and 1 <= len(s) <= 8


def _search_fmcsa_carrier(query, max_results=10):
    """Search FMCSA motor carrier database by company name.

    Requires web key (free registration at https://mobile.fmcsa.dot.gov/QCDevsite/).
    Endpoint: https://mobile.fmcsa.dot.gov/qc/services/carriers/name/{name}

    Args:
        query: Company name to search for
        max_results: Max results (API caps at 50)

    Returns:
        dict with matching carriers and their safety data
    """
    if not FMCSA_WEB_KEY:
        return {
            "source": "fmcsa",
            "skipped": True,
            "reason": "No FMCSA web key configured. Register at https://mobile.fmcsa.dot.gov/QCDevsite/"
        }

    encoded_name = urllib.parse.quote(query)
    size = min(max_results, 50)
    url = (f"https://mobile.fmcsa.dot.gov/qc/services/carriers/name/"
           f"{encoded_name}?webKey={FMCSA_WEB_KEY}&size={size}")

    result = _http_get_json(url, timeout=25)
    if "_error" in result:
        return {"source": "fmcsa", "error": result["_error"], "query": query}

    content = result.get("content", [])
    if not isinstance(content, list):
        content = [content] if content else []

    records = []
    for item in content[:max_results]:
        c = item.get("carrier", {})
        if not c:
            continue
        records.append({
            "dot_number": c.get("dotNumber", ""),
            "legal_name": c.get("legalName", ""),
            "dba_name": c.get("dbaName", ""),
            "allowed_to_operate": c.get("allowedToOperate", ""),
            "status_code": c.get("statusCode", ""),
            "city": c.get("phyCity", ""),
            "state": c.get("phyState", ""),
            "street": c.get("phyStreet", ""),
            "zip": c.get("phyZipcode", ""),
            "total_drivers": c.get("totalDrivers", ""),
            "total_power_units": c.get("totalPowerUnits", ""),
            "crash_total": c.get("crashTotal", 0),
            "fatal_crash": c.get("fatalCrash", 0),
            "inj_crash": c.get("injCrash", 0),
            "vehicle_insp": c.get("vehicleInsp", 0),
            "vehicle_oos_rate": c.get("vehicleOosRate", 0),
            "driver_insp": c.get("driverInsp", 0),
            "driver_oos_rate": c.get("driverOosRate", 0),
            "safety_rating": c.get("safetyRating", ""),
            "safety_rating_date": c.get("safetyRatingDate", ""),
            "ein": c.get("ein", ""),
            "is_passenger_carrier": c.get("isPassengerCarrier", ""),
        })

    return {
        "source": "fmcsa",
        "query": query,
        "returned": len(records),
        "records": records,
    }


def _get_fmcsa_carrier_by_dot(dot_number):
    """Look up a specific FMCSA carrier by DOT number.

    Returns comprehensive carrier data including safety record,
    insurance, and authority information.

    Args:
        dot_number: USDOT number (string or int)

    Returns:
        dict with full carrier details
    """
    if not FMCSA_WEB_KEY:
        return {
            "source": "fmcsa",
            "skipped": True,
            "reason": "No FMCSA web key configured."
        }

    dot_number = str(dot_number).strip()
    url = (f"https://mobile.fmcsa.dot.gov/qc/services/carriers/"
           f"{dot_number}?webKey={FMCSA_WEB_KEY}")

    result = _http_get_json(url, timeout=25)
    if "_error" in result:
        return {"source": "fmcsa", "error": result["_error"], "dot_number": dot_number}

    content = result.get("content", {})
    if isinstance(content, list):
        content = content[0] if content else {}

    c = content.get("carrier", {})
    if not c:
        return {
            "source": "fmcsa",
            "dot_number": dot_number,
            "error": "Carrier not found",
        }

    carrier_op = c.get("carrierOperation", {})

    detail = {
        "source": "fmcsa",
        "dot_number": c.get("dotNumber", dot_number),
        "legal_name": c.get("legalName", ""),
        "dba_name": c.get("dbaName", ""),
        "ein": c.get("ein", ""),
        "allowed_to_operate": c.get("allowedToOperate", ""),
        "status_code": c.get("statusCode", ""),
        "operation_type": carrier_op.get("carrierOperationDesc", ""),
        "is_passenger_carrier": c.get("isPassengerCarrier", ""),
        # Address
        "address": {
            "street": c.get("phyStreet", ""),
            "city": c.get("phyCity", ""),
            "state": c.get("phyState", ""),
            "zip": c.get("phyZipcode", ""),
            "country": c.get("phyCountry", ""),
        },
        # Fleet
        "total_drivers": c.get("totalDrivers", ""),
        "total_power_units": c.get("totalPowerUnits", ""),
        # Safety record
        "crash_total": c.get("crashTotal", 0),
        "fatal_crash": c.get("fatalCrash", 0),
        "inj_crash": c.get("injCrash", 0),
        "towaway_crash": c.get("towawayCrash", 0),
        # Inspections
        "vehicle_insp": c.get("vehicleInsp", 0),
        "vehicle_oos_rate": c.get("vehicleOosRate", 0),
        "vehicle_oos_national_avg": c.get("vehicleOosRateNationalAverage", ""),
        "driver_insp": c.get("driverInsp", 0),
        "driver_oos_rate": c.get("driverOosRate", 0),
        "driver_oos_national_avg": c.get("driverOosRateNationalAverage", ""),
        "hazmat_insp": c.get("hazmatInsp", 0),
        "hazmat_oos_rate": c.get("hazmatOosRate", 0),
        # Safety rating
        "safety_rating": c.get("safetyRating", ""),
        "safety_rating_date": c.get("safetyRatingDate", ""),
        "safety_review_date": c.get("safetyReviewDate", ""),
        # Authority / Insurance
        "common_authority_status": c.get("commonAuthorityStatus", ""),
        "contract_authority_status": c.get("contractAuthorityStatus", ""),
        "broker_authority_status": c.get("brokerAuthorityStatus", ""),
        "bipd_insurance_on_file": c.get("bipdInsuranceOnFile", ""),
        "bipd_required_amount": c.get("bipdRequiredAmount", ""),
        "cargo_insurance_on_file": c.get("cargoInsuranceOnFile", ""),
        "mcs150_outdated": c.get("mcs150Outdated", ""),
        "review_date": c.get("reviewDate", ""),
    }

    return detail


def _search_patents(query, max_results=10):
    """Search Google Patents (free, no auth required).

    Uses the Google Patents XHR endpoint which returns structured JSON
    with patent IDs, titles, inventors, assignees, and dates.

    Args:
        query: Inventor name, company name, or keywords
        max_results: Max results to return (default 10, max 100)

    Returns:
        dict with patent search results
    """
    encoded_query = urllib.parse.quote(query)
    url = f"https://patents.google.com/xhr/query?url=q%3D{encoded_query}&exp=&num={min(max_results, 100)}"

    req = urllib.request.Request(url)
    req.add_header("User-Agent",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.0.0 Safari/537.36")

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return {"source": "google_patents", "error": f"HTTP {e.code}: {e.reason}", "query": query}
    except urllib.error.URLError as e:
        return {"source": "google_patents", "error": f"URL error: {e.reason}", "query": query}
    except socket.timeout:
        return {"source": "google_patents", "error": f"Timeout after {REQUEST_TIMEOUT}s", "query": query}
    except json.JSONDecodeError as e:
        return {"source": "google_patents", "error": f"JSON parse error: {e}", "query": query}
    except Exception as e:
        return {"source": "google_patents", "error": str(e), "query": query}

    total = data.get("results", {}).get("total_num_results", 0)
    records = []
    for cluster in data.get("results", {}).get("cluster", []):
        for r in cluster.get("result", []):
            p = r.get("patent", {})
            patent_id = r.get("id", "")
            # Build a direct URL to Google Patents
            patent_url = f"https://patents.google.com/{patent_id}" if patent_id else ""
            records.append({
                "patent_id": patent_id.replace("patent/", "").replace("/en", ""),
                "title": (p.get("title", "") or "").replace("&hellip;", "...").strip(),
                "inventors": p.get("inventor", ""),
                "assignee": p.get("assignee", ""),
                "publication_date": p.get("publication_date", ""),
                "snippet": (p.get("snippet", "") or "")[:300],
                "url": patent_url,
            })

    return {
        "source": "google_patents",
        "query": query,
        "total_hits": total,
        "returned": len(records),
        "records": records[:max_results],
    }


def _search_sec_edgar(query, forms=None, max_results=10):
    """Search SEC EDGAR full-text search (EFTS).

    Free, no auth required. Just needs User-Agent with contact email.
    Endpoint: https://efts.sec.gov/LATEST/search-index

    Args:
        query: Search terms (company name, person name, keywords)
        forms: Optional comma-separated form types (e.g. "10-K,10-Q,8-K")
        max_results: Max results to return (default 10)

    Returns:
        dict with hits from EDGAR filings
    """
    if not _SEC_CONFIGURED:
        return {"source": "sec_edgar", "skipped": True,
                "detail": "SEC_USER_AGENT not configured -- set it in .env with your contact email per SEC EDGAR policy"}

    params = {
        "q": f'"{query}"',
        "dateRange": "custom",
        "startdt": "2000-01-01",
        "enddt": datetime.now().strftime("%Y-%m-%d"),
    }
    if forms:
        params["forms"] = forms

    url = f"https://efts.sec.gov/LATEST/search-index?{urllib.parse.urlencode(params)}"

    result = _http_get_json(url, headers={"User-Agent": SEC_USER_AGENT})

    if "_error" in result:
        return {"source": "sec_edgar", "error": result["_error"], "query": query}

    hits = result.get("hits", {}).get("hits", [])
    total = result.get("hits", {}).get("total", {}).get("value", 0)

    records = []
    for hit in hits[:max_results]:
        src = hit.get("_source", {})
        # entity_name may be empty; display_names has the useful data
        entity = src.get("entity_name", "") or ""
        display = src.get("display_names", [])
        if not entity and display:
            entity = display[0].split("(CIK")[0].strip().rstrip("(").strip()
        # EFTS uses 'form' not 'form_type', 'ciks' array not 'entity_id',
        # and 'adsh' (accession number) for building filing URLs
        form_type = src.get("form", "") or src.get("form_type", "") or src.get("file_type", "")
        ciks = src.get("ciks", [])
        primary_cik = ciks[0] if ciks else ""
        adsh = src.get("adsh", "")
        # Build a direct link to the filing on EDGAR
        if adsh:
            adsh_path = adsh.replace("-", "")
            filing_url = f"https://www.sec.gov/Archives/edgar/data/{primary_cik}/{adsh_path}/{adsh}-index.htm"
        elif primary_cik:
            filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={primary_cik}&type={form_type}"
        else:
            filing_url = ""
        records.append({
            "entity_name": entity,
            "display_names": display,
            "file_date": src.get("file_date", ""),
            "form_type": form_type,
            "file_description": src.get("file_description", ""),
            "filing_url": filing_url,
            "cik": primary_cik,
        })

    return {
        "source": "sec_edgar",
        "query": query,
        "total_hits": total,
        "returned": len(records),
        "records": records,
    }


def _search_sec_company(query, max_results=10):
    """Search SEC EDGAR company database by name.

    Uses the company search JSON endpoint. Free, no auth required.

    Args:
        query: Company name to search for
        max_results: Max results

    Returns:
        dict with matching companies and their CIK numbers
    """
    if not _SEC_CONFIGURED:
        return {"source": "sec_company", "skipped": True,
                "detail": "SEC_USER_AGENT not configured -- set it in .env with your contact email per SEC EDGAR policy"}

    # The company tickers endpoint returns all companies -- we filter client-side
    # For targeted search, use the submissions endpoint once we have a CIK
    # But first try the company search HTML endpoint and parse it
    # Actually, the best free approach is EFTS + company tickers lookup

    # Use the company search endpoint
    params = {
        "company": query,
        "CIK": "",
        "type": "",
        "dateb": "",
        "owner": "include",
        "count": str(max_results),
        "search_text": "",
        "action": "getcompany",
        "output": "atom",
    }

    url = f"https://www.sec.gov/cgi-bin/browse-edgar?{urllib.parse.urlencode(params)}"

    # This returns Atom XML, not JSON -- parse minimally
    req = urllib.request.Request(url)
    req.add_header("User-Agent", SEC_USER_AGENT)
    req.add_header("Accept", "application/atom+xml")

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = resp.read().decode("utf-8", errors="replace")

        # Simple XML parsing -- extract company entries
        import re
        entries = []
        # Find <entry> blocks
        for entry_match in re.finditer(r"<entry>(.*?)</entry>", data, re.DOTALL):
            entry_xml = entry_match.group(1)
            name_m = re.search(r"<title[^>]*>(.*?)</title>", entry_xml)
            cik_m = re.search(r"CIK=(\d+)", entry_xml)
            sic_m = re.search(r"SIC=(\d+)", entry_xml)
            state_m = re.search(r"State=(\w+)", entry_xml)

            if name_m:
                entries.append({
                    "name": name_m.group(1).strip(),
                    "cik": cik_m.group(1) if cik_m else "",
                    "sic": sic_m.group(1) if sic_m else "",
                    "state": state_m.group(1) if state_m else "",
                })

        return {
            "source": "sec_company_search",
            "query": query,
            "total": len(entries),
            "companies": entries[:max_results],
        }
    except Exception as e:
        return {"source": "sec_company_search", "error": str(e), "query": query}


def _search_opencorporates(query, jurisdiction=None, max_results=10):
    """Search OpenCorporates company registry.

    Requires API token (free: 50 requests/day).
    Endpoint: https://api.opencorporates.com/v0.4/companies/search

    Args:
        query: Company name
        jurisdiction: Optional jurisdiction code (e.g. "us_az" for Arizona)
        max_results: Max results

    Returns:
        dict with matching companies
    """
    if not OPENCORPORATES_API_TOKEN:
        return {
            "source": "opencorporates",
            "skipped": True,
            "reason": "No API token configured. Register at https://opencorporates.com/users/sign_up"
        }

    params = {
        "q": query,
        "per_page": str(min(max_results, 30)),
        "api_token": OPENCORPORATES_API_TOKEN,
    }
    if jurisdiction:
        params["jurisdiction_code"] = jurisdiction

    url = f"https://api.opencorporates.com/v0.4/companies/search?{urllib.parse.urlencode(params)}"
    result = _http_get_json(url)

    if "_error" in result:
        return {"source": "opencorporates", "error": result["_error"], "query": query}

    companies_raw = result.get("results", {}).get("companies", [])
    records = []
    for c in companies_raw[:max_results]:
        co = c.get("company", {})
        records.append({
            "name": co.get("name", ""),
            "company_number": co.get("company_number", ""),
            "jurisdiction": co.get("jurisdiction_code", ""),
            "status": co.get("current_status", ""),
            "incorporation_date": co.get("incorporation_date", ""),
            "company_type": co.get("company_type", ""),
            "registered_address": co.get("registered_address_in_full", ""),
            "opencorporates_url": co.get("opencorporates_url", ""),
        })

    return {
        "source": "opencorporates",
        "query": query,
        "total": result.get("results", {}).get("total_count", 0),
        "returned": len(records),
        "records": records,
    }


def _search_courtlistener(query, max_results=10):
    """Search CourtListener court records.

    Requires API token (free: 5K requests/day).
    Endpoint: https://www.courtlistener.com/api/rest/v4/search/

    Args:
        query: Search terms (name, case name, keywords)
        max_results: Max results

    Returns:
        dict with matching court records
    """
    if not COURTLISTENER_API_TOKEN:
        return {
            "source": "courtlistener",
            "skipped": True,
            "reason": "No API token configured. Register at https://www.courtlistener.com/sign-in/"
        }

    params = {
        "q": query,
        "format": "json",
        "page_size": str(min(max_results, 20)),
    }

    url = f"https://www.courtlistener.com/api/rest/v4/search/?{urllib.parse.urlencode(params)}"
    result = _http_get_json(url, headers={
        "Authorization": f"Token {COURTLISTENER_API_TOKEN}"
    })

    if "_error" in result:
        return {"source": "courtlistener", "error": result["_error"], "query": query}

    results_list = result.get("results", [])
    records = []
    for r in results_list[:max_results]:
        records.append({
            "case_name": r.get("caseName", r.get("case_name", "")),
            "court": r.get("court", ""),
            "date_filed": r.get("dateFiled", r.get("date_filed", "")),
            "docket_number": r.get("docketNumber", r.get("docket_number", "")),
            "status": r.get("status", ""),
            "url": r.get("absolute_url", ""),
        })

    return {
        "source": "courtlistener",
        "query": query,
        "total": result.get("count", len(records)),
        "returned": len(records),
        "records": records,
    }


# Map state abbreviations to OpenCorporates jurisdiction codes
_STATE_TO_JURISDICTION = {
    "AZ": "us_az", "CA": "us_ca", "NY": "us_ny", "TX": "us_tx",
    "FL": "us_fl", "IL": "us_il", "DE": "us_de", "NV": "us_nv",
    "WA": "us_wa", "CO": "us_co", "MA": "us_ma", "PA": "us_pa",
    "OH": "us_oh", "GA": "us_ga", "NC": "us_nc", "MI": "us_mi",
    "NJ": "us_nj", "VA": "us_va", "TN": "us_tn", "MO": "us_mo",
    "MD": "us_md", "WI": "us_wi", "MN": "us_mn", "OR": "us_or",
    "SC": "us_sc", "KY": "us_ky", "OK": "us_ok", "CT": "us_ct",
    "IA": "us_ia", "UT": "us_ut", "AR": "us_ar", "MS": "us_ms",
    "KS": "us_ks", "NM": "us_nm", "NE": "us_ne", "HI": "us_hi",
    "ID": "us_id", "WV": "us_wv", "ME": "us_me", "NH": "us_nh",
    "RI": "us_ri", "MT": "us_mt", "SD": "us_sd", "ND": "us_nd",
    "AK": "us_ak", "VT": "us_vt", "WY": "us_wy", "DC": "us_dc",
    "AL": "us_al", "IN": "us_in", "LA": "us_la",
}


def _dispatch_source(key, ctx):
    """Dispatch a search to the appropriate function for a registry source.

    Args:
        key: Source key from SOURCE_REGISTRY
        ctx: Search context dict with: query, max_results, forms, jurisdiction,
             model_year, query_is_vin, query_is_dot

    Returns:
        Result dict from the source's search function
    """
    q = ctx["query"]
    mr = ctx["max_results"]

    if key == "sec":
        return _search_sec_edgar(q, forms=ctx.get("forms"), max_results=mr)
    elif key == "sec_company":
        return _search_sec_company(q, max_results=mr)
    elif key == "patents":
        return _search_patents(q, max_results=mr)
    elif key == "opencorporates":
        return _search_opencorporates(q, jurisdiction=ctx.get("jurisdiction"), max_results=mr)
    elif key == "courtlistener":
        return _search_courtlistener(q, max_results=mr)
    elif key == "nhtsa":
        if not ctx.get("query_is_vin"):
            return {
                "source": "nhtsa_vin", "skipped": True,
                "reason": f"Query '{q}' doesn't look like a VIN (need 17 alphanumeric chars)"
            }
        return _decode_nhtsa_vin(q, model_year=ctx.get("model_year"))
    elif key == "fmcsa":
        if ctx.get("query_is_dot"):
            return _get_fmcsa_carrier_by_dot(q)
        return _search_fmcsa_carrier(q, max_results=mr)
    else:
        return {"source": key, "error": f"Unknown source '{key}' -- not in SOURCE_REGISTRY"}


def _resolve_sources(record_types, query_is_vin, query_is_dot):
    """Determine which sources to search based on record_types and query pattern.

    Returns:
        set of source keys to query
    """
    if not record_types or record_types.strip().lower() == "all":
        sources = {k for k, v in SOURCE_REGISTRY.items() if v["default_on"]}
        # Auto-include sources triggered by query pattern
        for key, spec in SOURCE_REGISTRY.items():
            trigger = spec.get("auto_include")
            if trigger == "vin" and query_is_vin:
                sources.add(key)
            elif trigger == "dot" and query_is_dot:
                sources.add(key)
        return sources
    return {s.strip().lower() for s in record_types.split(",")}


def search_records_impl(query=None, name=None, record_types=None, state=None,
                        forms=None, max_results=10, model_year=None,
                        driver=None, **kwargs):
    """Search public record databases for entity information.

    Args:
        query: Search terms (company name, person name, keywords, or VIN).
               Alias: 'name' (for backward compatibility with plan spec).
               If query is a 17-char VIN, NHTSA decode is auto-included.
        name: Alias for query
        record_types: Comma-separated list of sources to search.
            Options: sec, sec_company, patents, opencorporates, courtlistener, nhtsa, fmcsa, all
            Default: "all" (searches every available source except nhtsa/fmcsa unless query triggers auto-detect)
        state: State filter for jurisdiction-aware searches (e.g. "AZ")
        forms: SEC form types to filter (e.g. "10-K,10-Q,8-K")
        max_results: Max results per source (default 10)
        model_year: Optional model year for NHTSA VIN decode accuracy
        driver: Ignored (accepted for dispatch compatibility)

    Returns:
        dict with results from each queried source
    """
    search_query = query or name
    if not search_query:
        return {"error": "No search query provided. Pass 'query' or 'name' parameter."}

    max_results = min(int(max_results or 10), 25)

    query_is_vin = _is_vin(search_query)
    query_is_dot = _is_dot_number(search_query)
    jurisdiction = _STATE_TO_JURISDICTION.get(state.upper()) if state else None

    sources = _resolve_sources(record_types, query_is_vin, query_is_dot)

    # Build search context for dispatch
    ctx = {
        "query": search_query,
        "max_results": max_results,
        "forms": forms,
        "jurisdiction": jurisdiction,
        "model_year": model_year,
        "query_is_vin": query_is_vin,
        "query_is_dot": query_is_dot,
    }

    results = {"query": search_query, "sources_queried": [], "sources_skipped": []}

    # Iterate the registry in definition order, dispatching each requested source
    for key, spec in SOURCE_REGISTRY.items():
        if key not in sources:
            continue

        result_key = spec["result_key"]
        r = _dispatch_source(key, ctx)

        results[result_key] = r
        if r.get("skipped") or r.get("error"):
            results["sources_skipped"].append(result_key)
        else:
            results["sources_queried"].append(result_key)

    results["summary"] = {
        "queried": len(results["sources_queried"]),
        "skipped": len(results["sources_skipped"]),
        "total_records": sum(
            len(results.get(s, {}).get("records", results.get(s, {}).get("companies", [])))
            for s in results["sources_queried"]
        ),
    }

    return results


def main():
    """Subprocess entry point."""
    if len(sys.argv) < 2:
        print("ERROR: Missing params file path", file=sys.stderr)
        sys.exit(1)

    try:
        with open(sys.argv[1], 'r', encoding='utf-8') as f:
            p = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to load params file: {e}", file=sys.stderr)
        sys.exit(1)

    result = search_records_impl(**p)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
