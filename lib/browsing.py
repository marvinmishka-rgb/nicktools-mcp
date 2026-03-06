"""
Layer 1 -- Rate limiting, cache config, browse infrastructure.

Depends on: lib.paths (Layer 0).
Absorbs rate_limiter.py + BROWSE_* constants from config.py.
"""
import json
import time
from pathlib import Path
from lib.paths import BROWSE_STATE_DIR


# -- Constants --

BROWSE_RATE_FILE = BROWSE_STATE_DIR / "rate_limits.json"
BROWSE_CACHE_DIR = BROWSE_STATE_DIR / "cache"
BROWSE_DEFAULT_DELAY = 15   # seconds between requests to same domain
BROWSE_CACHE_TTL = 3600     # cache entries valid for 1 hour
BROWSE_MAX_RETRIES = 3      # max retries on 403/429/timeout


# -- Rate State Persistence --

def load_rate_state(rate_file: Path = None) -> dict:
    """Load per-domain rate state from JSON file."""
    rate_file = rate_file or BROWSE_RATE_FILE
    if rate_file.exists():
        try:
            return json.loads(rate_file.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_rate_state(rate_file: Path, state: dict):
    """Persist rate state to JSON file."""
    rate_file.parent.mkdir(parents=True, exist_ok=True)
    rate_file.write_text(json.dumps(state, indent=2), encoding="utf-8")


# -- Rate Limiting --

def enforce_rate_limit(
    domain: str,
    rate_file: Path = None,
    default_delay: int = None,
    min_delay: int = None,
) -> dict:
    """Check rate limit and sleep if needed. Returns info dict.

    Args:
        domain: The domain being requested (e.g. 'corcoran.com')
        rate_file: Path to the shared rate_limits.json (default: BROWSE_RATE_FILE)
        default_delay: Base delay in seconds (default: BROWSE_DEFAULT_DELAY)
        min_delay: Override delay (None = auto-calculate)

    Returns:
        dict with keys: waited (bool), wait_seconds, effective_delay,
        request_count, was_blocked
    """
    rate_file = rate_file or BROWSE_RATE_FILE
    default_delay = default_delay or BROWSE_DEFAULT_DELAY

    state = load_rate_state(rate_file)
    domain_state = state.get(domain, {})
    last_request = domain_state.get("last_request", 0)
    request_count = domain_state.get("request_count", 0)
    was_blocked = domain_state.get("was_blocked", False)

    # Determine delay
    if min_delay is not None:
        effective_delay = min_delay
    elif was_blocked:
        effective_delay = default_delay * 4  # 60s if previously blocked
    elif request_count > 10:
        effective_delay = default_delay * 2  # 30s for heavy use
    else:
        effective_delay = default_delay

    # Enforce
    wait_needed = effective_delay - (time.time() - last_request)
    info = {
        "waited": False,
        "wait_seconds": 0,
        "effective_delay": effective_delay,
        "request_count": request_count,
        "was_blocked": was_blocked,
    }

    if wait_needed > 0:
        info["waited"] = True
        info["wait_seconds"] = round(wait_needed, 1)
        time.sleep(wait_needed)

    return info


def record_request(
    domain: str,
    rate_file: Path = None,
    was_blocked: bool = False,
):
    """Record that a request was made to update rate state.

    Call this AFTER the request completes (or fails).
    """
    rate_file = rate_file or BROWSE_RATE_FILE

    state = load_rate_state(rate_file)
    domain_state = state.get(domain, {})
    request_count = domain_state.get("request_count", 0)

    state[domain] = {
        "last_request": time.time(),
        "request_count": request_count + 1,
        "was_blocked": was_blocked,
    }
    if was_blocked:
        state[domain]["blocked_at"] = time.time()

    save_rate_state(rate_file, state)
