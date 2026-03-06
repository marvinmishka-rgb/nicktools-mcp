"""Direct Chrome CDP page capture -- subprocess entry point for capture.py Tier 2.

Launched as a subprocess by capture.py._tier2_nodriver(). Inherits no state
from the parent process. Communicates via stdin (JSON params) / temp file (JSON result).

Uses Chrome's remote debugging protocol (CDP) directly instead of nodriver,
which has a Windows bug where its --remote-debugging-host=127.0.0.1 flag
causes WinError 10049 connection failures despite Chrome listening correctly.

IMPORTANT: This script is fully synchronous -- no asyncio.run(). On Windows,
asyncio's ProactorEventLoop interferes with urllib connections to 127.0.0.1
when VPN split tunneling is active. Using websockets.sync.client avoids this.

This script:
  1. Finds a free port
  2. Launches Chrome headless with --remote-debugging-port
  3. Waits for CDP to be ready (HTTP polling)
  4. Connects via synchronous WebSocket
  5. Navigates to the target URL and waits for content
  6. Extracts title, text, and full HTML
  7. Writes result to a temp file (avoids stdout pollution from Chrome)

Intentionally minimal -- no caching, no rate limiting, no retry logic.
Those concerns belong to capture.py and the tools that call it.
"""
import json
import sys
import os
import socket
import subprocess
import tempfile
import time
import warnings

warnings.filterwarnings("ignore", category=ResourceWarning)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def find_free_port():
    """Find an available TCP port on localhost."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def capture(url, wait_seconds=3):
    """Launch Chrome, navigate to URL, extract content via CDP. Fully synchronous."""
    import shutil

    port = find_free_port()
    user_data_dir = tempfile.mkdtemp(prefix="cdp_capture_")

    chrome_args = [
        CHROME_PATH,
        f"--remote-debugging-port={port}",
        "--headless=new",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-sync",
        f"--user-agent={USER_AGENT}",
        "about:blank",
    ]

    proc = subprocess.Popen(
        chrome_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        # Wait for CDP to become available AND get the page target WS URL.
        # This polls until Chrome is fully ready (both /json/version and
        # /json targets respond), which avoids race conditions where Chrome
        # starts but page targets aren't yet available.
        page_ws = _wait_for_cdp_page(port, timeout=10)
        if not page_ws:
            return _fail(f"cdp-connect-failed (port={port}, pid={proc.pid})")

        # Extract content via synchronous WebSocket CDP
        return _cdp_extract_sync(page_ws, url, wait_seconds)

    except Exception as e:
        return _fail(f"capture-error: {str(e)[:200]}")
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        shutil.rmtree(user_data_dir, ignore_errors=True)


def _wait_for_cdp_page(port, timeout=10):
    """Wait for Chrome's CDP to be fully ready and return the page target's WS URL.

    Polls both /json/version (CDP alive) and /json (page targets available)
    in a single loop, returning only when we have a valid page WebSocket URL.
    This avoids race conditions where Chrome is listening but page targets
    aren't yet registered.

    Returns page WebSocket URL or None on timeout.
    """
    import urllib.request

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            # First check: is CDP responding at all?
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2
            )

            # Second check: are page targets available?
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=2
            )
            targets = json.loads(resp.read())

            for t in targets:
                if t.get("type") == "page":
                    ws_url = t.get("webSocketDebuggerUrl", "")
                    if ws_url:
                        return ws_url

            # CDP is up but no page target yet -- keep polling
            time.sleep(0.3)

        except Exception:
            time.sleep(0.3)

    return None


def _cdp_extract_sync(ws_url, url, wait_seconds):
    """Connect to Chrome via synchronous WebSocket, navigate, and extract content.

    Uses websockets.sync.client (v16+) to avoid asyncio entirely.
    This prevents Windows ProactorEventLoop + VPN conflicts.

    Retries WebSocket connection up to 3 times to handle intermittent
    WinError 10049 from ExpressVPN's WFP hooks on loopback addresses.
    """
    from websockets.sync.client import connect

    # Retry WebSocket connection (VPN WFP interference causes intermittent 10049)
    ws = None
    last_error = None
    for attempt in range(3):
        try:
            ws = connect(ws_url, max_size=50_000_000)
            break
        except OSError as e:
            last_error = e
            time.sleep(0.5)
        except Exception as e:
            last_error = e
            break  # Non-OSError failures won't benefit from retry

    if ws is None:
        return _fail(f"ws-connect-failed ({last_error})")

    try:
        with ws:
            cmd_id = 1

            def send_cmd(method, params=None):
                nonlocal cmd_id
                msg = {"id": cmd_id, "method": method}
                if params:
                    msg["params"] = params
                cmd_id += 1
                ws.send(json.dumps(msg))

                # Wait for matching response (skip events)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    raw = ws.recv(timeout=15)
                    resp = json.loads(raw)
                    if resp.get("id") == msg["id"]:
                        return resp.get("result", {})
                    # Skip CDP events (method field, no id)
                return {}

            # Navigate to URL
            send_cmd("Page.enable")
            send_cmd("Page.navigate", {"url": url})

            # Wait for page to load and JS to execute
            time.sleep(wait_seconds)

            # Extract content
            title_result = send_cmd(
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True}
            )
            text_result = send_cmd(
                "Runtime.evaluate",
                {"expression": "document.body ? document.body.innerText : ''",
                 "returnByValue": True}
            )
            url_result = send_cmd(
                "Runtime.evaluate",
                {"expression": "window.location.href", "returnByValue": True}
            )
            html_result = send_cmd(
                "Runtime.evaluate",
                {"expression": "document.documentElement.outerHTML",
                 "returnByValue": True}
            )

            title = (title_result.get("result", {}).get("value", "") or "")
            text = (text_result.get("result", {}).get("value", "") or "")
            final_url = (url_result.get("result", {}).get("value", "") or url)
            html = (html_result.get("result", {}).get("value", "") or "")

            return {
                "success": True,
                "html": html,
                "text": text,
                "title": title,
                "final_url": final_url,
                "error": None,
            }

    except ImportError:
        return _fail("websockets-not-installed: pip install websockets")
    except Exception as e:
        return _fail(f"ws-error: {str(e)[:200]}")


def _fail(error):
    """Build a standardized failure result."""
    return {
        "success": False,
        "html": "",
        "text": "",
        "title": "",
        "final_url": "",
        "error": error,
    }


def main():
    params = json.loads(sys.stdin.read())
    url = params.get("url", "")
    wait_seconds = params.get("wait_seconds", 3)
    result_file = params.get("result_file", "")

    if not result_file:
        print(json.dumps({"success": False, "error": "No result_file provided"}))
        sys.exit(1)

    # Suppress stdout (Chrome may print messages)
    sys.stdout = open(os.devnull, 'w')
    sys.stderr = open(os.devnull, 'w')

    result = capture(url, wait_seconds)

    # Write result to temp file
    with open(result_file, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False)

    sys.exit(0)


if __name__ == "__main__":
    main()
