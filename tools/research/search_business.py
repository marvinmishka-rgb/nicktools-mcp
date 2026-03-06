"""
search_business -- Search state business entity registries.

Generic framework with state-specific adapters. Searches by entity name,
principal name, or registered agent name across supported state registries.

Currently supported states:
  - UT (Utah): businessregistration.utah.gov
  - AZ (Arizona): ecorp.azcc.gov (planned)

Architecture:
  1. HTTP POST to state search endpoint (fast, ~2s)
  2. Parse HTML response for entity table
  3. Optionally fetch entity details (principals, agent, addresses)
  4. If HTTP fails (JS-only states), fall back to Chrome --dump-dom

Designed as an in-process tool (_impl function) for fast dispatch.
---
description: Search state business entity registries (UT, AZ) by name or principal
databases: []
read_only: true
optional: true
domain_extension: State business registry search -- scrapes public state corporation databases. Example of building state-specific adapters. Currently supports UT (Utah). Safe to remove if not needed.
---
"""
import json
import re
import sys
import http.cookiejar
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

# Try importing browser module for Chrome fallback
try:
    from lib.browser import fetch_rendered, extract_tables, extract_text, submit_form_http
    HAS_BROWSER = True
except ImportError:
    HAS_BROWSER = False

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20


# ======================================================================
# Generic framework
# ======================================================================

# State adapter registry -- each adapter provides search() and detail()
STATE_ADAPTERS = {}


def register_state(code):
    """Decorator to register a state adapter."""
    def decorator(cls):
        STATE_ADAPTERS[code.upper()] = cls
        return cls
    return decorator


class StateAdapter:
    """Base class for state business search adapters."""

    state_code = ""
    state_name = ""
    base_url = ""
    search_types = ["entity_name"]

    def search(self, query, search_type="entity_name", max_results=10, **kwargs):
        raise NotImplementedError

    def detail(self, entity_id, **kwargs):
        raise NotImplementedError


# ======================================================================
# Utah adapter
# ======================================================================

@register_state("UT")
class UtahAdapter(StateAdapter):
    """Utah Division of Corporations -- businessregistration.utah.gov

    Two-step process:
      1. POST search form -> HTML table with entity list
      2. POST detail form -> HTML page with entity info, principals, agent

    Both endpoints require session cookies from an initial page load.
    Uses http.cookiejar for automatic cookie management.
    """

    state_code = "UT"
    state_name = "Utah"
    base_url = "https://businessregistration.utah.gov"
    search_types = ["entity_name", "principal_name", "agent_name", "entity_number"]

    def _create_opener(self):
        """Create a urllib opener with cookie handling."""
        cj = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def _init_session(self, opener):
        """Load the search page to get session cookies."""
        url = f"{self.base_url}/EntitySearch/OnlineEntitySearch"
        req = urllib.request.Request(url)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "text/html")
        try:
            with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
                resp.read()  # consume response, we just need the cookies
            return True
        except Exception as e:
            return False

    def _build_search_form(self, query, search_type):
        """Build the Utah search form data."""
        form = {
            "QuickSearch.BusinessId": "",
            "QuickSearch.NVBusinessNumber": "",
            "QuickSearch.StartsWith": "false",
            "QuickSearch.Contains": "true",
            "QuickSearch.ExactMatch": "false",
            "QuickSearch.Allwords": "",
            "QuickSearch.BusinessName": "",
            "QuickSearch.PrincipalName": "",
            "QuickSearch.DomicileName": "",
            "QuickSearch.AssumedName": "",
            "QuickSearch.AgentName": "",
            "QuickSearch.MarkNumber": "",
            "QuickSearch.Classification": "",
            "QuickSearch.FilingNumber": "",
            "QuickSearch.Goods": "",
            "QuickSearch.ApplicantName": "",
            "QuickSearch.All": "",
            "QuickSearch.EntitySearch": "true",
            "QuickSearch.MarkSearch": "",
            "QuickSearch.SeqNo": "0",
            "AdvancedSearch.BusinessTypeID": "",
            "AdvancedSearch.BusinessTypes": "",
            "AdvancedSearch.BusinessStatusID": "0",
            "AdvancedSearch.StatusDetails": "",
            "AdvancedSearch.BusinessSubTypes": "",
            "AdvancedSearch.JurdisctionTypeID": "",
            "AdvancedSearch.IncludeInactive": "true",
            "AdvancedSearch.EntityDateFrom": "",
            "AdvancedSearch.EntityDateTo": "",
            "AdvancedSearch.StatusDateFrom": "",
            "AdvancedSearch.StatusDateTo": "",
        }

        field_map = {
            "entity_name": "QuickSearch.BusinessName",
            "principal_name": "QuickSearch.PrincipalName",
            "agent_name": "QuickSearch.AgentName",
            "entity_number": "QuickSearch.BusinessId",
        }
        field = field_map.get(search_type, "QuickSearch.BusinessName")
        form[field] = query
        return form

    def _parse_search_results(self, html, max_results=10):
        """Parse the Utah search results HTML table."""
        results = []

        # Find entity rows: onclick = GetBusinessSearchResultById("11711043","0")
        # Columns: Name | Other Name | Filing Date/Time | Status | Status Details | File Date | Type | Subtype | Entity Number
        pattern = re.compile(
            r'GetBusinessSearchResultById\("(\d+)","(\d+)"\)\s*'
            r'[^>]*>([^<]+)</a>'
            r'.*?'
            r'<td[^>]*>([^<]*)</td>'   # other name (DBA)
            r'\s*<td[^>]*>([^<]*)</td>' # filing date/time
            r'\s*<td[^>]*>([^<]*)</td>' # status
            r'\s*<td[^>]*>([^<]*)</td>' # status details
            r'\s*<td[^>]*>([^<]*)</td>' # file date
            r'\s*<td[^>]*>([^<]*)</td>' # type
            r'\s*<td[^>]*>([^<]*)</td>' # subtype
            r'\s*<td[^>]*>([^<]*)</td>', # entity number
            re.DOTALL
        )

        for m in pattern.finditer(html):
            if len(results) >= max_results:
                break
            row = {
                "business_id": m.group(1),
                "entity_name": m.group(3).strip(),
                "entity_number": m.group(11).strip(),
                "entity_type": m.group(9).strip(),
                "entity_subtype": m.group(10).strip(),
                "status": m.group(6).strip(),
                "status_details": m.group(7).strip(),
                "filing_date": m.group(5).strip(),
                "file_date": m.group(8).strip(),
            }
            other_name = m.group(4).strip()
            if other_name:
                row["other_name"] = other_name
            results.append(row)

        # Also get total count
        total_match = re.search(r'records\s+\d+\s+to\s+\d+\s+of\s+(\d+)', html)
        total = int(total_match.group(1)) if total_match else len(results)

        return results, total

    def _parse_detail(self, html):
        """Parse the Utah entity detail HTML page."""
        detail = {}

        # Entity name
        m = re.search(r'Entity Name:\s*</label>\s*</div>\s*<div[^>]*>\s*([^<]+)', html)
        if m:
            detail["entity_name"] = m.group(1).strip()

        # Entity number
        m = re.search(r'Entity Number:\s*</label>\s*</div>\s*<div[^>]*>\s*([^<]+)', html)
        if m:
            detail["entity_number"] = m.group(1).strip()

        # Entity type
        m = re.search(r'Entity Type:\s*</label>\s*</div>\s*<div[^>]*>\s*([^<]+)', html)
        if m:
            detail["entity_type"] = m.group(1).strip()

        # Status
        m = re.search(r'Entity Status:\s*</label>\s*</div>\s*<div[^>]*>\s*([^<]+)', html)
        if m:
            detail["status"] = m.group(1).strip()

        # Status details
        m = re.search(r'Entity Status Details:\s*</label>\s*</div>\s*<div[^>]*>\s*([\w\s]+)', html)
        if m:
            detail["status_details"] = m.group(1).strip()

        # Formation date
        m = re.search(r'Formation Date:\s*</label>\s*</div>\s*<div[^>]*>\s*([\d/]+)', html)
        if m:
            detail["formation_date"] = m.group(1).strip()

        # Registered agent
        agent_section = re.search(
            r'(?:Registered|Statutory)\s+AGENT\s+INFORMATION.*?'
            r'Name:\s*</label>\s*</div>\s*<div[^>]*>\s*([^<]+).*?'
            r'(?:Street Address|Address):\s*</label>\s*</div>\s*<div[^>]*>\s*([^<]+)',
            html, re.DOTALL | re.IGNORECASE
        )
        if agent_section:
            detail["registered_agent"] = {
                "name": agent_section.group(1).strip(),
                "address": agent_section.group(2).strip(),
            }

        # Principals -- extract from the table
        detail["principals"] = []
        principal_section = re.search(
            r'PRINCIPAL\s+INFORMATION(.*?)(?:ADDRESS\s+INFORMATION|SERVICE\s+OF\s+PROCESS|Filing\s+History)',
            html, re.DOTALL | re.IGNORECASE
        )
        if principal_section:
            # Each principal row: title, name, address, date
            for pm in re.finditer(
                r'<tr[^>]*>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>',
                principal_section.group(1)
            ):
                title = pm.group(1).strip()
                name = pm.group(2).strip()
                addr = pm.group(3).strip()
                date = pm.group(4).strip()
                if name and title not in ("Title", ""):
                    detail["principals"].append({
                        "title": title,
                        "name": name,
                        "address": addr,
                        "last_updated": date,
                    })

        # Physical address
        m = re.search(
            r'Physical Address:\s*</label>\s*</div>\s*<div[^>]*>\s*([^<]+)',
            html, re.DOTALL
        )
        if m:
            detail["physical_address"] = m.group(1).strip()

        # Mailing address
        m = re.search(
            r'Mailing Address:\s*</label>\s*</div>\s*<div[^>]*>\s*([^<]+)',
            html, re.DOTALL
        )
        if m and m.group(1).strip():
            detail["mailing_address"] = m.group(1).strip()

        return detail

    def search(self, query, search_type="entity_name", max_results=10, **kwargs):
        """Search Utah business entities."""
        opener = self._create_opener()

        # Step 1: Initialize session (get cookies)
        if not self._init_session(opener):
            # Fallback to Chrome if HTTP session init fails
            if HAS_BROWSER:
                return self._search_via_chrome(query, search_type, max_results)
            return {"error": "Failed to initialize Utah session and Chrome fallback not available"}

        # Step 2: POST search form
        form_data = self._build_search_form(query, search_type)
        search_url = f"{self.base_url}/EntitySearch/OnlineBusinessAndMarkSearchResult"

        encoded = urllib.parse.urlencode(form_data).encode("utf-8")
        req = urllib.request.Request(search_url, data=encoded)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Accept", "text/html")
        req.add_header("Referer", f"{self.base_url}/EntitySearch/OnlineEntitySearch")

        try:
            with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            if HAS_BROWSER:
                return self._search_via_chrome(query, search_type, max_results)
            return {"error": f"Utah search request failed: {e}"}

        # Step 3: Parse results
        entities, total = self._parse_search_results(html, max_results)

        return {
            "state": "UT",
            "query": query,
            "search_type": search_type,
            "total": total,
            "returned": len(entities),
            "entities": entities,
            "method": "http-post",
        }

    def detail(self, entity_id, **kwargs):
        """Fetch detailed entity information from Utah."""
        # Parse entity_id -- could be "11711043" or "11711043-0160"
        business_id = entity_id.split("-")[0] if "-" in entity_id else entity_id

        opener = self._create_opener()
        if not self._init_session(opener):
            return {"error": "Failed to initialize Utah session"}

        detail_url = f"{self.base_url}/EntitySearch/BusinessInformation"
        form_data = {"businessId": business_id, "businessReservationNumber": "0"}

        encoded = urllib.parse.urlencode(form_data).encode("utf-8")
        req = urllib.request.Request(detail_url, data=encoded)
        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Accept", "text/html")
        req.add_header("Referer", f"{self.base_url}/EntitySearch/OnlineBusinessAndMarkSearchResult")

        try:
            with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            return {"error": f"Utah detail request failed: {e}"}

        detail = self._parse_detail(html)
        detail["state"] = "UT"
        detail["business_id"] = business_id
        detail["method"] = "http-post"
        return detail

    def _search_via_chrome(self, query, search_type, max_results):
        """Fallback: use Chrome --dump-dom for the search."""
        # Can't easily submit forms via headless Chrome CLI
        # But we can at least dump the search page for diagnostic purposes
        result = fetch_rendered(
            f"{self.base_url}/EntitySearch/OnlineEntitySearch",
            wait_ms=8000
        )
        if not result.get("success"):
            return {"error": f"Chrome fallback also failed: {result.get('error')}"}

        return {
            "state": "UT",
            "query": query,
            "search_type": search_type,
            "error": "HTTP search failed; Chrome rendered the search page but cannot submit forms. Use Chrome DevTools MCP for interactive search.",
            "method": "chrome-fallback",
        }


# ======================================================================
# Arizona adapter (stub -- to be implemented)
# ======================================================================

@register_state("AZ")
class ArizonaAdapter(StateAdapter):
    """Arizona Corporation Commission -- ecorp.azcc.gov

    TODO: Implement. Requires Chrome rendering (SPA).
    """

    state_code = "AZ"
    state_name = "Arizona"
    base_url = "https://ecorp.azcc.gov"
    search_types = ["entity_name"]

    def search(self, query, search_type="entity_name", max_results=10, **kwargs):
        if not HAS_BROWSER:
            return {
                "state": "AZ",
                "error": "Arizona search requires Chrome rendering (not yet implemented). Use Chrome DevTools MCP.",
            }

        # Try Chrome --dump-dom on the search URL with query param
        search_url = f"{self.base_url}/EntitySearch/Index"
        result = fetch_rendered(search_url, wait_ms=8000, timeout=30)

        if not result.get("success"):
            return {"state": "AZ", "error": f"Chrome failed: {result.get('error')}"}

        # AZ is a full SPA -- dump-dom gets the shell but not search results
        return {
            "state": "AZ",
            "query": query,
            "error": "Arizona eCorp is a JavaScript SPA. Chrome --dump-dom captures the shell but cannot submit the search form. Use Chrome DevTools MCP for interactive search.",
            "method": "chrome-dump-dom",
            "note": "Full AZ adapter requires browser automation (Phase 5 lib/browser.py upgrade)",
        }

    def detail(self, entity_id, **kwargs):
        return {"state": "AZ", "error": "Arizona detail not yet implemented"}


# ======================================================================
# Main implementation
# ======================================================================

def search_business_impl(query=None, state=None, search_type="entity_name",
                         entity_id=None, max_results=10, driver=None, **kwargs):
    """Search state business entity registries.

    Args:
        query: Entity name, principal name, or agent name to search for.
               Not needed if entity_id is provided.
        state (required): Two-letter state code (e.g. "UT", "AZ")
        search_type: What to search by. Options vary by state:
            - entity_name (default): Search by business name
            - principal_name: Search by principal/member/officer name
            - agent_name: Search by registered agent name
            - entity_number: Search by entity registration number
        entity_id: If provided, fetch detailed entity info instead of searching.
                   Format varies by state (e.g. "11711043" or "11711043-0160" for UT).
        max_results: Max search results to return (default 10)
        driver: Ignored (dispatch compatibility)

    Returns:
        dict with search results or entity details

    Examples:
        # Search Utah by entity name
        research("search_business", {"query": "1-Call Handyman", "state": "UT"})

        # Search Utah by principal name
        research("search_business", {"query": "William Robinson", "state": "UT", "search_type": "principal_name"})

        # Get entity details
        research("search_business", {"state": "UT", "entity_id": "11711043"})
    """
    if not state:
        supported = sorted(STATE_ADAPTERS.keys())
        return {"error": f"Missing required parameter: state. Supported states: {supported}"}

    state = state.strip().upper()
    adapter_cls = STATE_ADAPTERS.get(state)

    if not adapter_cls:
        supported = sorted(STATE_ADAPTERS.keys())
        return {"error": f"State '{state}' not supported. Supported: {supported}"}

    adapter = adapter_cls()

    # Detail mode
    if entity_id:
        return adapter.detail(entity_id)

    # Search mode
    if not query:
        return {"error": "Missing required parameter: query (or entity_id for detail lookup)"}

    if search_type not in adapter.search_types:
        return {
            "error": f"Search type '{search_type}' not supported for {state}. "
                     f"Supported: {adapter.search_types}"
        }

    max_results = min(int(max_results or 10), 25)
    return adapter.search(query, search_type=search_type, max_results=max_results)


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

    result = search_business_impl(**p)
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
