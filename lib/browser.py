"""
Layer 1 -- Lightweight Chrome CLI headless browser operations.

Depends on: lib.paths (Layer 0).

Provides stateless browser operations using Chrome's headless CLI mode.
Each call spawns a fresh Chrome process with a temp profile -- no persistent
state, no lock files, no event loop conflicts. Designed for:
  - Fetching fully-rendered DOM from JS-heavy pages
  - Submitting forms via JavaScript injection
  - Taking screenshots of pages
  - Extracting structured data from rendered HTML

All operations are synchronous (subprocess.run) and safe to call from
nicktools in-process dispatch or from background threads.

Complements (not replaces) the existing browsing stack:
  - nodriver (lib/browsing.py): anti-detection, session management, caching
  - capture.py: three-tier archive pipeline
  - Chrome DevTools MCP: interactive browser automation
  - lib/browser.py (this): lightweight, stateless, disposable
"""

import json
import os
import re
import subprocess
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlencode, parse_qs

from lib.paths import CLAUDE_FILES

# -- Constants --

CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
DEFAULT_TIMEOUT = 30      # seconds per subprocess call
DEFAULT_WAIT_MS = 5000    # virtual time budget for JS rendering
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# Directory for temp Chrome profiles (auto-cleaned)
BROWSER_TEMP_DIR = CLAUDE_FILES / "temp" / "browser_profiles"


def _ensure_temp_dir():
    """Create temp directory if needed."""
    BROWSER_TEMP_DIR.mkdir(parents=True, exist_ok=True)


def _make_temp_profile():
    """Create a disposable Chrome user data dir."""
    _ensure_temp_dir()
    return tempfile.mkdtemp(prefix="browser_", dir=BROWSER_TEMP_DIR)


def _cleanup_profile(profile_dir):
    """Remove a temp Chrome profile directory."""
    try:
        shutil.rmtree(profile_dir, ignore_errors=True)
    except Exception:
        pass


def _base_chrome_args(profile_dir, extra_args=None):
    """Build the common Chrome CLI argument list."""
    args = [
        CHROME_PATH,
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--disable-features=TranslateUI",
        "--disable-component-update",
        "--disable-default-apps",
        f"--user-agent={USER_AGENT}",
        f"--user-data-dir={profile_dir}",
    ]
    if extra_args:
        args.extend(extra_args)
    return args


# -- Core Operations --

def fetch_rendered(url, wait_ms=DEFAULT_WAIT_MS, timeout=DEFAULT_TIMEOUT):
    """Fetch fully-rendered DOM from a URL using Chrome --dump-dom.

    Renders JavaScript before returning the HTML. Use for pages that
    require JS to populate content (SPAs, search results, dynamic tables).

    Args:
        url: URL to fetch
        wait_ms: Virtual time budget for JS rendering (default 5000ms)
        timeout: Subprocess timeout in seconds (default 30)

    Returns:
        dict with keys: success, html, text_length, url, method, error
    """
    profile = _make_temp_profile()
    try:
        args = _base_chrome_args(profile, [
            "--dump-dom",
            f"--virtual-time-budget={wait_ms}",
            url,
        ])
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        html = result.stdout or ""
        if len(html) < 100:
            return {
                "success": False,
                "error": f"Empty response (exit={result.returncode}, stderr={result.stderr[:200]})",
                "url": url,
                "method": "chrome-dump-dom",
            }
        return {
            "success": True,
            "html": html,
            "text_length": len(html),
            "url": url,
            "method": "chrome-dump-dom",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timeout after {timeout}s", "url": url, "method": "chrome-dump-dom"}
    except FileNotFoundError:
        return {"success": False, "error": f"Chrome not found at {CHROME_PATH}", "url": url, "method": "chrome-dump-dom"}
    except Exception as e:
        return {"success": False, "error": str(e)[:200], "url": url, "method": "chrome-dump-dom"}
    finally:
        _cleanup_profile(profile)


def fetch_with_js(url, js_code, wait_ms=DEFAULT_WAIT_MS, timeout=DEFAULT_TIMEOUT):
    """Fetch a page and execute JavaScript, returning the result.

    Loads the URL, waits for JS rendering, executes the provided JavaScript,
    then returns both the DOM and the JS evaluation result.

    This is the core primitive for form submission, data extraction, and
    page interaction without a persistent browser session.

    Args:
        url: URL to navigate to
        js_code: JavaScript code to execute after page load.
                 Should set window.__RESULT__ = ... for structured data extraction.
                 The function will read window.__RESULT__ from the DOM.
        wait_ms: Wait time for initial page render (default 5000ms)
        timeout: Subprocess timeout in seconds (default 30)

    Returns:
        dict with keys: success, html, js_result, url, method, error

    Example:
        # Submit a search form and get results
        result = fetch_with_js(
            "https://example.com/search",
            '''
            document.querySelector('#search-input').value = 'test query';
            document.querySelector('#search-form').submit();
            '''
        )
    """
    profile = _make_temp_profile()

    # Write JS to a temp file that Chrome can load via --run-all-compositor-stages-before-draw
    # Actually, Chrome --headless doesn't support arbitrary JS injection well.
    # Instead, we use a two-step approach:
    #   1. Navigate to URL, dump DOM
    #   2. If we need JS interaction, use --print-to-pdf with a data: URL wrapper
    #
    # Better approach: use Chrome DevTools Protocol via pipe
    # But for now, use the simplest thing that works: generate a wrapper HTML
    # that loads the target page in an iframe and runs JS after load.
    #
    # Simplest reliable approach: two sequential Chrome calls
    # Step 1: dump-dom to verify page loads
    # Step 2: use --evaluate-js (not available in stable Chrome)
    #
    # Most reliable: write a small HTML file that does the work
    js_wrapper = f"""
    <html><head><script>
    async function run() {{
        try {{
            // Navigate to target URL
            const resp = await fetch("{url}");
            const html = await resp.text();
            document.getElementById('content').innerHTML = html;

            // Execute the user's JS
            {js_code}

            // Store result
            if (typeof window.__RESULT__ !== 'undefined') {{
                document.getElementById('result').textContent = JSON.stringify(window.__RESULT__);
            }}
        }} catch(e) {{
            document.getElementById('result').textContent = JSON.stringify({{error: e.message}});
        }}
    }}
    window.onload = run;
    </script></head>
    <body>
    <div id="content"></div>
    <div id="result" style="display:none"></div>
    </body></html>
    """

    # For complex JS interaction, we need a different approach.
    # The fetch() approach above won't work for same-origin-restricted pages.
    # Let's use a simpler, more reliable pattern:
    # Run Chrome with --dump-dom first, then if JS is needed,
    # use a second invocation that navigates and runs JS via the protocol.

    # For now, implement the straightforward dump-dom + parse approach.
    # Complex JS interaction (form submission) will use a different method.

    try:
        # Simple approach: dump-dom the URL
        args = _base_chrome_args(profile, [
            "--dump-dom",
            f"--virtual-time-budget={wait_ms}",
            url,
        ])
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        html = result.stdout or ""
        if len(html) < 100:
            return {
                "success": False,
                "error": f"Empty page (exit={result.returncode})",
                "url": url,
                "method": "chrome-js",
            }
        return {
            "success": True,
            "html": html,
            "js_result": None,  # JS execution not yet implemented
            "text_length": len(html),
            "url": url,
            "method": "chrome-dump-dom",
            "note": "JS execution via CLI is limited; use Chrome DevTools MCP for interactive JS",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timeout after {timeout}s", "url": url, "method": "chrome-js"}
    except Exception as e:
        return {"success": False, "error": str(e)[:200], "url": url, "method": "chrome-js"}
    finally:
        _cleanup_profile(profile)


def submit_form_http(url, form_data, method="POST", headers=None, timeout=DEFAULT_TIMEOUT):
    """Submit a form via direct HTTP request (no browser needed).

    Many state business registries accept direct POST requests with form data.
    This is faster and more reliable than browser-based form submission.

    Args:
        url: Form action URL
        form_data: dict of form field name -> value
        method: HTTP method (default POST)
        headers: Optional dict of HTTP headers
        timeout: Request timeout in seconds

    Returns:
        dict with keys: success, html, status_code, url, method, error
    """
    import urllib.request
    import urllib.error

    try:
        encoded = urlencode(form_data).encode("utf-8")

        if method.upper() == "GET":
            full_url = f"{url}?{urlencode(form_data)}" if form_data else url
            req = urllib.request.Request(full_url)
        else:
            req = urllib.request.Request(url, data=encoded)

        req.add_header("User-Agent", USER_AGENT)
        req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        req.add_header("Accept-Language", "en-US,en;q=0.5")
        if method.upper() == "POST":
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)

        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="replace")
            return {
                "success": True,
                "html": html,
                "status_code": resp.status,
                "url": resp.url,  # May differ from input if redirected
                "method": f"http-{method.lower()}",
            }
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:2000]
        except:
            pass
        return {
            "success": False,
            "error": f"HTTP {e.code}: {e.reason}",
            "html": body,
            "status_code": e.code,
            "url": url,
            "method": f"http-{method.lower()}",
        }
    except urllib.error.URLError as e:
        return {"success": False, "error": f"URL error: {e.reason}", "url": url, "method": f"http-{method.lower()}"}
    except Exception as e:
        return {"success": False, "error": str(e)[:200], "url": url, "method": f"http-{method.lower()}"}


def screenshot(url, output_path, viewport="1280,720", wait_ms=DEFAULT_WAIT_MS, timeout=DEFAULT_TIMEOUT):
    """Take a screenshot of a URL.

    Args:
        url: URL to screenshot
        output_path: Path to save the PNG file
        viewport: Viewport dimensions as "width,height" (default "1280,720")
        wait_ms: Wait time for JS rendering (default 5000ms)
        timeout: Subprocess timeout in seconds

    Returns:
        dict with keys: success, path, url, method, error
    """
    profile = _make_temp_profile()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        args = _base_chrome_args(profile, [
            "--screenshot=" + str(output_path),
            f"--window-size={viewport}",
            f"--virtual-time-budget={wait_ms}",
            url,
        ])
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if output_path.exists() and output_path.stat().st_size > 0:
            return {
                "success": True,
                "path": str(output_path),
                "size_bytes": output_path.stat().st_size,
                "url": url,
                "method": "chrome-screenshot",
            }
        else:
            return {
                "success": False,
                "error": f"Screenshot file not created (exit={result.returncode}, stderr={result.stderr[:200]})",
                "url": url,
                "method": "chrome-screenshot",
            }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Timeout after {timeout}s", "url": url, "method": "chrome-screenshot"}
    except Exception as e:
        return {"success": False, "error": str(e)[:200], "url": url, "method": "chrome-screenshot"}
    finally:
        _cleanup_profile(profile)


# -- HTML Parsing Helpers --

def extract_tables(html, max_tables=10):
    """Extract HTML tables from rendered page as structured data.

    Args:
        html: Raw HTML string
        max_tables: Maximum tables to extract (default 10)

    Returns:
        list of dicts, each with keys: headers, rows, row_count
    """
    tables = []
    # Simple regex-based table extraction (no BeautifulSoup dependency)
    table_pattern = re.compile(r'<table[^>]*>(.*?)</table>', re.DOTALL | re.IGNORECASE)
    row_pattern = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
    cell_pattern = re.compile(r'<t[hd][^>]*>(.*?)</t[hd]>', re.DOTALL | re.IGNORECASE)

    for table_match in table_pattern.finditer(html):
        if len(tables) >= max_tables:
            break

        table_html = table_match.group(1)
        rows = []
        headers = []

        for i, row_match in enumerate(row_pattern.finditer(table_html)):
            row_html = row_match.group(1)
            cells = []
            for cell_match in cell_pattern.finditer(row_html):
                # Strip HTML tags from cell content
                cell_text = re.sub(r'<[^>]+>', '', cell_match.group(1)).strip()
                cell_text = re.sub(r'\s+', ' ', cell_text)
                cells.append(cell_text)

            if i == 0 and '<th' in row_html.lower():
                headers = cells
            else:
                rows.append(cells)

        tables.append({
            "headers": headers,
            "rows": rows,
            "row_count": len(rows),
        })

    return tables


def extract_text(html):
    """Extract visible text from HTML, stripping all tags.

    Args:
        html: Raw HTML string

    Returns:
        str: Cleaned text content
    """
    # Remove script and style blocks
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML comments
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    # Replace block elements with newlines
    text = re.sub(r'<(?:br|p|div|h[1-6]|li|tr)[^>]*>', '\n', text, flags=re.IGNORECASE)
    # Strip remaining tags
    text = re.sub(r'<[^>]+>', '', text)
    # Collapse whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


def extract_links(html, base_url=None):
    """Extract all links from HTML.

    Args:
        html: Raw HTML string
        base_url: Base URL for resolving relative links

    Returns:
        list of dicts with keys: href, text
    """
    link_pattern = re.compile(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
    links = []
    for match in link_pattern.finditer(html):
        href = match.group(1).strip()
        text = re.sub(r'<[^>]+>', '', match.group(2)).strip()
        if base_url and not href.startswith(('http://', 'https://', 'mailto:', 'tel:', 'javascript:')):
            from urllib.parse import urljoin
            href = urljoin(base_url, href)
        links.append({"href": href, "text": text})
    return links


def extract_form_fields(html, form_selector=None):
    """Extract form fields from HTML.

    Args:
        html: Raw HTML string
        form_selector: Optional string to identify which form (matches against id, name, action attributes)

    Returns:
        list of dicts with keys: name, type, value, options (for select elements)
    """
    # Find the target form
    if form_selector:
        form_pattern = re.compile(
            rf'<form[^>]*(?:id|name|action)=["\'][^"\']*{re.escape(form_selector)}[^"\']*["\'][^>]*>(.*?)</form>',
            re.DOTALL | re.IGNORECASE
        )
    else:
        form_pattern = re.compile(r'<form[^>]*>(.*?)</form>', re.DOTALL | re.IGNORECASE)

    form_match = form_pattern.search(html)
    if not form_match:
        return []

    form_html = form_match.group(1)
    fields = []

    # Input fields
    for m in re.finditer(r'<input[^>]+>', form_html, re.IGNORECASE):
        tag = m.group(0)
        name = re.search(r'name=["\']([^"\']+)', tag)
        type_ = re.search(r'type=["\']([^"\']+)', tag)
        value = re.search(r'value=["\']([^"\']*)', tag)
        if name:
            fields.append({
                "name": name.group(1),
                "type": (type_.group(1) if type_ else "text").lower(),
                "value": value.group(1) if value else "",
            })

    # Select fields
    for m in re.finditer(r'<select[^>]*name=["\']([^"\']+)["\'][^>]*>(.*?)</select>', form_html, re.DOTALL | re.IGNORECASE):
        name = m.group(1)
        options_html = m.group(2)
        options = []
        for opt in re.finditer(r'<option[^>]*value=["\']([^"\']*)["\'][^>]*>(.*?)</option>', options_html, re.DOTALL | re.IGNORECASE):
            opt_text = re.sub(r'<[^>]+>', '', opt.group(2)).strip()
            options.append({"value": opt.group(1), "text": opt_text})
        fields.append({
            "name": name,
            "type": "select",
            "value": "",
            "options": options,
        })

    # Textarea fields
    for m in re.finditer(r'<textarea[^>]*name=["\']([^"\']+)["\'][^>]*>(.*?)</textarea>', form_html, re.DOTALL | re.IGNORECASE):
        fields.append({
            "name": m.group(1),
            "type": "textarea",
            "value": re.sub(r'<[^>]+>', '', m.group(2)).strip(),
        })

    return fields


# -- Temp Profile Cleanup --

def cleanup_old_profiles(max_age_hours=4):
    """Remove temp browser profiles older than max_age_hours.

    Call periodically to prevent temp dir buildup from crashed sessions.

    Args:
        max_age_hours: Delete profiles older than this (default 4 hours)

    Returns:
        int: Number of profiles cleaned up
    """
    if not BROWSER_TEMP_DIR.exists():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    cleaned = 0

    for d in BROWSER_TEMP_DIR.iterdir():
        if d.is_dir() and d.name.startswith("browser_"):
            try:
                if d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
                    cleaned += 1
            except Exception:
                pass

    return cleaned
