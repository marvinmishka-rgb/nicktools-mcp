"""
Layer 0 -- Audit log parser for Cowork session analysis.

Streaming parser for audit.jsonl files. Extracts tool counts, keywords,
domain signals, entity mentions, and session statistics without loading
entire files into memory.

No internal dependencies except paths (Layer 0).
"""

import json
import re
from collections import Counter
from pathlib import Path

from lib.paths import ARCHIVES_DIR

# Archive location for session backups
SESSION_ARCHIVE_DIR = ARCHIVES_DIR / "cowork-sessions"

# --- Stopwords for keyword extraction ---
# Common English + Claude/tool noise words that don't carry meaning
STOPWORDS = frozenset("""
    the a an and or but in on at to for of is it its this that with from by
    as are was were be been being have has had do does did will would could
    should can may might shall not no nor so if then than too very just about
    up out into over after before between through during above below each
    few more most other some such only same also back even still new now old
    well way long use make like time just know take people come these than
    them been many what when where which who how all any both each more most
    some such here there when where why how all well also get got can will
    yes okay sure right want need let see look thing things going think
    going want need something really actually here there pretty much
    claude please thank thanks help could would should shall think
    okay want need let see look here there something actually
    file files tool tools error result results output data info
    you your yours yourself we our ours they their them his her
    one two first last next don does doesn didn wasn weren isn
    using used try trying got made sure already done work working
    set add added run running read write check update create
""".split())

# Domain keywords that signal specific research/work domains.
# CUSTOMIZATION: Replace or extend these with keywords for your research domain.
# The audit watcher uses these to auto-detect which domain a session is working in.
DOMAIN_KEYWORDS = {
    "corcoran": {"corcoran", "chelsea", "highline", "gallery", "richard", "hamptons"},
    "tooling": {"tool", "nicktools", "mcp", "server", "dispatch", "lib", "script"},
    "operations": {"vm", "cowork", "hcs", "watchdog", "gpu", "tdr", "service", "process"},
    "research": {"research", "archive", "source", "investigate", "entity", "evidence"},
    "lifestream": {"lifestream", "entry", "stream", "session", "domain", "tag"},
    "neo4j": {"neo4j", "cypher", "node", "graph", "relationship", "apoc", "gds"},
}


# --- Streaming parser ---

def parse_audit_streaming(audit_path, since_line=0):
    """
    Yield parsed entries from audit.jsonl, optionally starting from line N.

    Yields dicts with at minimum: {"type": str, "line_num": int}
    Skips malformed lines silently.
    """
    with open(audit_path, encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f):
            if i < since_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entry["_line_num"] = i
                yield entry
            except (json.JSONDecodeError, ValueError):
                continue


def collect_entries(audit_path, since_line=0):
    """
    Collect all parsed entries into categorized lists.
    Returns: {"user": [...], "assistant": [...], "system": [...], "total_lines": int}

    For large files, prefer parse_audit_streaming() with on-the-fly processing.
    """
    user_msgs = []
    assistant_msgs = []
    system_msgs = []
    total = 0

    for entry in parse_audit_streaming(audit_path, since_line):
        total += 1
        t = entry.get("type")
        if t == "user":
            user_msgs.append(entry)
        elif t == "assistant":
            assistant_msgs.append(entry)
        elif t == "system":
            system_msgs.append(entry)

    return {
        "user": user_msgs,
        "assistant": assistant_msgs,
        "system": system_msgs,
        "total_lines": total,
    }


# --- Tool counts ---

def extract_tool_counts(entries_or_path, since_line=0):
    """
    Count tool calls from assistant messages.

    Args:
        entries_or_path: Either a list of assistant entry dicts,
                         or a path to audit.jsonl (will stream).
    Returns:
        Counter of {tool_name: count}
    """
    counts = Counter()

    if isinstance(entries_or_path, (str, Path)):
        for entry in parse_audit_streaming(str(entries_or_path), since_line):
            if entry.get("type") != "assistant":
                continue
            for block in entry.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    counts[block.get("name", "unknown")] += 1
    else:
        for entry in entries_or_path:
            for block in entry.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    counts[block.get("name", "unknown")] += 1

    return counts


# --- Keyword extraction -------------------------------------------------

def _tokenize(text):
    """Split text into lowercase alpha tokens, filtering noise."""
    return [w for w in re.findall(r"[a-zA-Z]{3,}", text.lower())
            if w not in STOPWORDS and len(w) < 30]


def extract_user_keywords(entries_or_path, top_n=20, since_line=0):
    """
    Frequency-based keyword extraction from user messages.
    No ML dependencies -- just tokenize, remove stopwords, count, rank.

    Args:
        entries_or_path: List of user entry dicts, or path to audit.jsonl.
        top_n: Number of top keywords to return.
    Returns:
        List of (keyword, count) tuples, sorted by frequency.
    """
    word_counts = Counter()

    def _process_user_entry(entry):
        msg = entry.get("message", {})
        content = msg.get("content", "")
        if isinstance(content, str):
            word_counts.update(_tokenize(content))
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    word_counts.update(_tokenize(block["text"]))

    if isinstance(entries_or_path, (str, Path)):
        for entry in parse_audit_streaming(str(entries_or_path), since_line):
            if entry.get("type") == "user":
                _process_user_entry(entry)
    else:
        for entry in entries_or_path:
            _process_user_entry(entry)

    return word_counts.most_common(top_n)


# --- Domain signal detection --------------------------------------------

def extract_domain_signals(entries_or_path, since_line=0):
    """
    Infer active domains from user messages and tool parameters.

    Scans for domain keywords in user text and tool call parameters
    (create_entry domains, archive URLs, graph operations).

    Returns: dict of {domain: score} where score = keyword hit count.
    """
    domain_scores = Counter()

    def _score_text(text):
        words = set(re.findall(r"[a-zA-Z]{3,}", text.lower()))
        for domain, keywords in DOMAIN_KEYWORDS.items():
            hits = words & keywords
            if hits:
                domain_scores[domain] += len(hits)

    def _process_entry(entry):
        t = entry.get("type")
        if t == "user":
            msg = entry.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, str):
                _score_text(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        _score_text(block["text"])

        elif t == "assistant":
            # Check tool call parameters for domain signals
            for block in entry.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    inp = block.get("input", {})
                    if isinstance(inp, dict):
                        # Check domains param in create_entry calls
                        domains = inp.get("domains")
                        if isinstance(domains, str):
                            for d in domains.split(","):
                                d = d.strip().lower()
                                if d in DOMAIN_KEYWORDS:
                                    domain_scores[d] += 3  # Direct domain mention = strong signal
                        # Check all string params for domain keywords
                        for v in inp.values():
                            if isinstance(v, str) and len(v) < 500:
                                _score_text(v)

    if isinstance(entries_or_path, (str, Path)):
        for entry in parse_audit_streaming(str(entries_or_path), since_line):
            _process_entry(entry)
    else:
        for entry in entries_or_path:
            _process_entry(entry)

    return dict(domain_scores.most_common())


# --- Session statistics -------------------------------------------------

def compute_session_stats(entries_or_path, since_line=0):
    """
    Compute session-level statistics.

    Returns:
        {
            "user_messages": int,
            "assistant_messages": int,
            "total_lines": int,
            "first_timestamp": str or None,
            "last_timestamp": str or None,
            "duration_minutes": float or None,
            "avg_response_length": int,
            "max_response_length": int,
            "tool_call_count": int,
        }
    """
    user_count = 0
    assistant_count = 0
    total_lines = 0
    first_ts = None
    last_ts = None
    response_lengths = []
    tool_call_count = 0

    def _process(entry):
        nonlocal user_count, assistant_count, total_lines, first_ts, last_ts, tool_call_count

        total_lines += 1
        ts = entry.get("_audit_timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        t = entry.get("type")
        if t == "user":
            user_count += 1
        elif t == "assistant":
            assistant_count += 1
            content = entry.get("message", {}).get("content", [])
            text_len = 0
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_len += len(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_call_count += 1
            if text_len > 0:
                response_lengths.append(text_len)

    if isinstance(entries_or_path, (str, Path)):
        for entry in parse_audit_streaming(str(entries_or_path), since_line):
            _process(entry)
    else:
        for entry in entries_or_path:
            _process(entry)

    # Compute duration
    duration = None
    if first_ts and last_ts:
        try:
            from datetime import datetime, timezone
            # Parse ISO timestamps
            ft = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            lt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration = round((lt - ft).total_seconds() / 60, 1)
        except Exception:
            pass

    return {
        "user_messages": user_count,
        "assistant_messages": assistant_count,
        "total_lines": total_lines,
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "duration_minutes": duration,
        "avg_response_length": (
            round(sum(response_lengths) / len(response_lengths))
            if response_lengths else 0
        ),
        "max_response_length": max(response_lengths) if response_lengths else 0,
        "tool_call_count": tool_call_count,
    }


# --- Full digest (combines all extractors) ------------------------------

def generate_digest(audit_path, known_entities=None, top_keywords=20):
    """
    Full session digest combining all extractors in a single pass.

    This is the main entry point for harvest_session's digest mode.
    Reads the audit file once and runs all analysis.

    Args:
        audit_path: Path to audit.jsonl
        known_entities: Optional list of entity names to match against
        top_keywords: Number of keywords to extract

    Returns:
        {
            "stats": {...},          # from compute_session_stats
            "tool_counts": {...},    # Counter as dict
            "keywords": [...],       # list of (word, count) tuples
            "domain_signals": {...}, # {domain: score}
            "entity_mentions": [...] # matched entity names (if known_entities provided)
        }
    """
    # Collect all entries once
    collected = collect_entries(audit_path)

    # Run extractors on collected data
    stats = compute_session_stats(
        collected["user"] + collected["assistant"] + collected["system"]
    )
    stats["total_lines"] = collected["total_lines"]

    tool_counts = extract_tool_counts(collected["assistant"])
    keywords = extract_user_keywords(collected["user"], top_n=top_keywords)
    domains = extract_domain_signals(
        collected["user"] + collected["assistant"]
    )

    # Entity mention detection (optional)
    entity_mentions = []
    if known_entities:
        entity_mentions = _find_entity_mentions(
            collected["user"] + collected["assistant"],
            known_entities
        )

    return {
        "stats": stats,
        "tool_counts": dict(tool_counts),
        "keywords": keywords,
        "domain_signals": domains,
        "entity_mentions": entity_mentions,
    }


def _find_entity_mentions(entries, known_entities):
    """
    Find mentions of known entity names in user and assistant text.

    Simple substring matching (case-insensitive) against known entity names.
    Returns list of {"name": str, "count": int} sorted by count.
    """
    mention_counts = Counter()
    # Build lowercase lookup
    entity_lower = {name.lower(): name for name in known_entities}

    for entry in entries:
        content = entry.get("message", {}).get("content", "")
        if isinstance(content, str):
            text = content.lower()
        elif isinstance(content, list):
            text = " ".join(
                block.get("text", "").lower()
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            continue

        for lower_name, orig_name in entity_lower.items():
            if lower_name in text:
                mention_counts[orig_name] += 1

    return [{"name": name, "count": count}
            for name, count in mention_counts.most_common()]
