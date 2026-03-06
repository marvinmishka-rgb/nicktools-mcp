# How nicktools Was Built: 15 Days of Human-AI Architecture

nicktools wasn't designed on a whiteboard. It emerged from real research failures, operational friction, and iterative refinement across 41 collaborative sessions between a human researcher and Claude. This is the story of how that happened — and what we learned about building durable infrastructure through human-AI collaboration.

*Grounded in 43 CoworkSession nodes, 267 lifestream entries, and 199 MB of audit logs across 54 sessions.*

## The Problem (February 15, 2026)

The starting point was a fragmented ecosystem: five different execution environments (a Linux VM, Windows PowerShell, Neo4j direct queries, Chrome DevTools, and nothing for reliable Python execution), no persistent memory between sessions, and a research workflow that lost context every time a conversation hit its token limit. Research findings were ephemeral. Architectural decisions were forgotten and re-debated. Sources were read but never archived.

The core insight: **a research system without provenance is just a text generator with extra steps.** If you can't trace a claim back to a source, verify when it was captured, or recover context from a previous session, you're not doing research — you're doing search.

## Phase 1: A Unified Environment (Feb 17-23)

The first real problem was Python stdout. Running Python through the Windows MCP shell produced silence — scripts would execute successfully but all output vanished into the void. The underlying cause: when an MCP server owns stdin/stdout for JSON-RPC protocol messages, child processes inherit those file descriptors and deadlock.

Four versions of the server went by before the fix landed: `stdin=subprocess.DEVNULL` on child process calls. One line of code, four days of learning why it mattered.

In parallel, Chrome DevTools MCP hit a wall: bot detection on every site we actually needed to research. The response was nodriver — an anti-detection browser automation library that loaded protected pages when Chrome DevTools couldn't. This established a pattern that would recur: **when a tool fails, the right response isn't to retry harder — it's to add a fallback tier.**

By February 23, the five execution environments were coordinated under a single dispatcher. The tool registry tracked 20+ operations across all environments with consistent invocation patterns.

## Phase 2: The Archive Pipeline (Feb 23-25)

The research workflow exposed a critical bottleneck: archiving sources blocked everything. Each archive call took 5-30 seconds. When archiving failed (and it did, often — paywalls, bot detection, rate limiting), research degraded to unverified claims with no provenance chain.

The architectural solution: decouple capture from research. Queue URLs for archiving (instant, non-blocking), process them in background with a multi-tier fallback pipeline, and read the results when ready. The capture pipeline settled into four tiers:

1. **HTTP + readability-lxml** (~2 seconds, handles 80%+ of articles)
2. **Chrome CDP via WebSocket** (~5 seconds, JavaScript-rendered pages)
3. **Chrome CLI --dump-dom** (~10 seconds, reliable fallback)
4. **Wayback CDX API** (~5 seconds, safety net for blocked/dead pages)

Each tier only fires if the previous one fails. A paywall (401/403) skips the JavaScript tiers entirely — no point rendering a page you can't access. A homepage redirect (SPA pattern) escalates to JavaScript rendering. The pipeline is Claude-initiated: every result is visible, failures are diagnosable, and Source nodes are created atomically in the graph database.

## Phase 3: Self-Documenting Infrastructure (Feb 26-28)

After 23 sessions and 40,000+ tool calls, a different problem emerged: nobody could see what was working. Error patterns were invisible. Documentation was already stale. Context windows reset every 40 minutes, and each new context started from scratch.

The response was a self-documenting system. Reference docs auto-generate from live system state: tool inventory with signatures, Neo4j schema with property types and counts, research standards with provenance rules, playbook index with validation status. Run one command and all documentation regenerates from the source of truth.

Then came cross-context intelligence. A `session_health` tool accumulates error patterns across the server's entire lifetime — not a rolling window, but cumulative categorization. When a new context window starts, instead of re-discovering that a particular URL is blocked or a specific operation has a known failure mode, Claude asks the system: "what's already failed?" The system responds with categorized guidance.

This phase also established parallel call safety rules. We discovered that when one call in a parallel MCP batch fails, the platform kills all sibling calls in the same batch. One blocked URL was wasting 3-4 successful file reads. The solution was simple: web requests in one batch, file operations in another. Never mix failure-prone operations with reliable ones.

## Phase 4: Session Intelligence (Feb 28 - Mar 1)

Sessions were being created and discarded with no connective tissue. The question "what did I research last week?" had no answer.

The session harvester changed this. A daemon thread monitors the current session's audit log in real-time, extracting entity names, source URLs, error signals, and tool usage patterns. This data accumulates on CoworkSession nodes in the graph database. Sessions chain temporally via PRECEDED_BY edges. Entries link to sessions via PRODUCED_IN edges. Domains link to sessions via COVERED_TOPIC edges.

The result: three tiers of session context recovery. Graph traversal (fast, structured) finds which sessions produced which entries. Entry content (curated interpretation) provides 300-500 word analytical summaries. Audit mining (ground truth) provides complete conversation transcripts with all tool calls and reasoning chains.

A `session_recover` tool cross-references accumulated session data against the graph: what entities were mentioned but never committed? What sources were fetched but never archived? What errors recurred? This powers clean handoff between sessions — and between context windows within a session.

## Phase 5: The Unified Graph Interface (Mar 1-3)

By this point, the system had accumulated 28 separate MCP tool declarations, 6 redundant write paths, no unified read patterns, and a configuration module that mixed 6 unrelated concerns in 372 lines.

The v3 architecture consolidated everything:

**Five meta-tools** replaced 28 declarations: `graph()`, `research()`, `entry()`, `core()`, and `query()`. Each routes to operations via a registry. Adding a new operation costs zero MCP configuration changes — just drop a Python file in the right directory.

**Three library layers** with strict import rules: Layer 0 (paths, database, I/O, schema) has no internal dependencies. Layer 1 (URLs, entries, browsing, capture) depends only on Layer 0. Layer 2 (sources, archives, batch writes) depends on Layers 0-1. Adding a new tool is cheap; changing a library module is safe because all dependents are below you in the dependency graph.

**A unified write engine** replaced 6 entity-specific wrappers. It accepts entities in natural shape — nested relationships, sources, extra labels — validates against the schema registry, generates Cypher, and executes batched. One tool call can create a Person node with employment relationships, evidence links, and source provenance in a single atomic transaction.

## The Seven Most Interesting Technical Failures

### 1. The "Enforce Don't Instruct" Collapse

During a novel, high-engagement research session, every quality practice failed simultaneously. Archives were marked captured when empty. Sources were cited without being archived. Training-data hallucinations were attributed to real sources.

The root cause: the system relied on Claude remembering checklists during the exact moments when Claude was least likely to follow them — when doing novel analytical work. Every behavioral rule added to instructions made this worse because it increased the checklist without increasing enforcement.

The resolution became a design principle: if a quality standard can be enforced by code, it must not be left to instructions. This led to tool upgrades — capture validation in archiving tools, source confidence tiers in entry creation, automated quality audits. The role division was clarified: Claude thinks (patterns, connections, synthesis), tools enforce (validation gates, provenance tracking), humans direct (choosing targets, evaluating significance).

**Lesson:** Instruction-based quality control fails under cognitive load. Encode quality standards in tools, not documentation.

### 2. The 12 Duplicate Source Nodes

Twelve pairs of duplicate Source nodes appeared because raw Cypher MERGE statements used non-canonical URLs — same article, different URLs (with/without `www.`, trailing slashes, tracking parameters).

The fix: `canonicalize_url()` in the library layer strips www., trailing slashes, and tracking params. Retrofitted into all Source-node-creating tools. The duplicates were merged.

**Lesson:** When you have a canonical form for an identifier, enforce it at the library layer, not at the call site. Every raw MERGE is a potential duplicate.

### 3. The VPN/Browsing Capture Failures

Archive captures failed mysteriously — sites that should work returned empty content or connection errors. The failures were intermittent and seemed random.

The root cause was multi-layered: VPN exit nodes blocked certain sites, VPN IP ranges were cached in the rate limiter affecting residential sessions, and nodriver's debugging flags triggered socket errors because VPN hooks intercepted loopback connections. Each fix revealed the next layer. The final architecture (four-tier fallback) is more resilient than any single fix would have been.

**Lesson:** Intermittent failures often have multiple interacting causes. Build fallback architectures instead of hunting for a single root cause.

### 4. The Monolith That Couldn't Be Edited

By v0.8.0, server.py was 3,300 lines — every tool's Cypher queries, business logic, imports, and error handling in a single file. Editing one tool risked breaking others.

The v0.9.0 refactor decomposed it to a thin dispatcher plus standalone tool scripts. server.py dropped to 909 lines. Tool scripts could be edited without server restart. A bug was found and fixed in a tool script without restarting the server on the same day — proving the architecture's value on day one.

**Lesson:** Monoliths become painful faster than you expect. The extraction was mechanical (one session) but the productivity improvement was immediate.

### 5. The Speed Run That Broke Production

An attempt to do the entire 1.0 release preparation in a single session — editing the live system for release changes — broke production twice. The release plan document itself was born from this failure: a 9-phase plan with explicit rules about never editing the live system for release changes.

**Lesson:** "I'll just do it all at once" is the most expensive sentence in software development.

### 6. The Ghost Cohort False Positive

Analysis of Wayback Machine data showed 7 agents who appeared to join a brokerage in Q4 2019 and then vanished — a suspicious pattern that consumed multiple sessions. Cross-brokerage verification using newly-built anti-detection browsing revealed 3 of 7 were still active, 1 moved to another firm, and only 2 actually left. The "ghost" label was premature — it was based on one snapshot per agent, misinterpreted as brief tenure.

**Lesson:** Single-source analysis can create compelling but false narratives. The tool that debunked the hypothesis didn't exist when the hypothesis was formed. Building new capabilities changes what you can verify.

### 7. Context Window Cascade Failures

Audit log analysis revealed that sessions averaged 6-13 context resets, with ~110 tool calls per window. When one web request failed, the Cowork platform killed all sibling calls in the same parallel batch — so one blocked URL would waste 3-4 successful file reads.

The fix: cumulative error tracking that persists across resets, a `session_health` tool for bootstrapping awareness after each reset, and codified rules about never batching web requests with local file operations. The underlying insight: the server process persists even when Claude's context resets, so the server should accumulate intelligence that individual context windows cannot.

**Lesson:** In long-running AI sessions, the persistent server is the right place to accumulate operational intelligence. The LLM's context window is ephemeral; the server is not.

## Development Patterns

**Research drives tool development.** The most productive improvements came from running into friction during actual research — not from planning sessions. The archive pipeline was built because a specific investigation needed access to a specific site. The graph abstraction layer was built because manually wiring Cypher for each person node was too error-prone during a research sprint.

**Diagnose before building.** The best sessions started with measurement — gap analyses, archive audits, system evaluations using real research as test cases. The worst sessions started with "let's build" or "let's release."

**The incremental ratchet.** The system grew from 0 to 55 operations not through planned feature drops but through steady accumulation — a few operations per session, each tested before the next was started. Each version left the system strictly better: more capable, more self-aware, or both.

**Self-documentation as infrastructure.** Before the self-documenting system, documentation drifted from reality within days. After it, running one command regenerates all reference docs from live state. The key insight: factual data should be derived from the system; operational wisdom should be hand-authored.

## By the Numbers

- **41 sessions** over 15 days
- **58,000+ tool calls** executed
- **267 lifestream entries** documenting decisions, findings, and milestones
- **55 operations** across 5 meta-tools
- **19 library modules** across 3 dependency layers
- **5,276 knowledge graph nodes** with 9,699 relationships and 313 source references

## What We'd Do Differently

**Start with the library layer.** We built tools first and extracted the library later. This meant rewriting URL canonicalization, source wiring, and schema validation multiple times as they migrated from tool-specific code to shared library functions. A layered library design from day one would have prevented the 12 duplicate Source nodes and 22 bare label-less nodes that accumulated before validation was centralized.

**Decouple capture from the start.** The synchronous archive pipeline cost weeks of research friction before the async redesign. The pattern — queue submissions, process in background, read results when ready — is universally applicable. We should have built it first.

**Test against fresh environments earlier.** The system was developed and tested against a single Neo4j instance with existing data. Fresh-install behavior (missing indexes, no APOC triggers, empty databases) wasn't tested until late. The `ensure_apoc_triggers()` auto-repair function that runs at startup came from a real production failure.

## The Collaboration Pattern

This wasn't "AI codes while human watches." The dynamic was iterative and complementary:

1. Human hits a wall (Python stdout broken, research blocked by bot detection, archives too slow)
2. Claude prototypes a solution in an isolated session
3. Human reviews, identifies architectural implications, suggests improvements
4. Claude refactors based on feedback and documents the decision
5. Result captured in the lifestream as a milestone or decision entry
6. Next session builds on that foundation

The persistent memory system (the lifestream) made this sustainable across sessions. Without it, every context window reset would have been a hard restart. With it, each session could pick up where the last one left off — not by re-reading transcripts, but by querying the graph for what was decided, what was found, and what still needed work.

The system improves by accumulation of small optimizations across sessions, not by periodic rewrites. Every session should leave the system better than it found it.
