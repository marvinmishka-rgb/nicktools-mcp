> Auto-generated docs also available: `research("help")` for full reference, or `research("read")` (no params) for single-op docs.

# Research Operations

Web research, archiving, and source management. Tools for reading web pages, archiving evidence, managing the capture pipeline, and searching public records.

## Return Conventions

All research operations return JSON dicts with `snake_case` keys. Errors include an `"error"` key. Archive/capture operations include `status` and source metadata. Read operations return `text`, `url`, and capture tier info.

## Routing Guide

- **Read a web page?** -> `read` (four-tier capture: HTTP + Chrome + Wayback)
- **Read a bot-protected page?** -> `read` with `stealth: true` (nodriver anti-detection)
- **Read and archive in one step?** -> `read` with `archive: true`
- **Archive a page (auto mode)?** -> `archive` (default, uses four-tier pipeline + Source node)
- **Archive with full MHTML?** -> `archive` with `mode: "mhtml"` (CDP snapshot)
- **Archive with raw HTML preservation?** -> `archive` with `mode: "full"` (nodriver capture)
- **Queue URLs for batch archiving?** -> `queue_archive` (instant, non-blocking) -> `process_queue` (batch worker)
- **Check queue status?** -> `check_queue`
- **Read a completed capture?** -> `read_staged`
- **Paywalled HTML saved from browser?** -> `extract_saved_article` (strips JS paywall)
- **Ingest user-saved HTML files?** -> `ingest_saved` (from uploads/websites/)
- **PDF to search or extract?** -> `search_pdf` (local files or URLs)
- **Wayback Machine snapshot?** -> `wayback_lookup` (CDX API)
- **Check if archived sources are still live?** -> `check_sources`
- **Audit archive health?** -> `archive_inventory` (filesystem + Neo4j reconciliation)
- **Public records search?** -> `search_records` (SEC, patents, OpenCorporates, courts, NHTSA, FMCSA)
- **State business registry?** -> `search_business` (UT, AZ)
- **Decode a VIN?** -> `vin_decode` (NHTSA)
- **Generate a research report?** -> `generate_report` (markdown from graph data)

---

## read

**Primary reading tool.** Reads a URL and returns content using a four-tier capture pipeline. Replaces `fetch_page` and `browse_url` with a single intent-based operation.

Default path: HTTP+readability -> Chrome CDP -> Chrome CLI -> Wayback CDX. Includes paywall early-exit (401/403 skip JS tiers) and SPA escalation (homepage-redirect triggers JS rendering retry).

Stealth path (`stealth: true`): anti-detection browsing via nodriver with response caching and per-domain rate limiting. Use for bot-protected sites that block the default HTTP tier.

**Parameters:**
- **url** (required): URL to read
- **format**: Output format -- `"text"` (default, clean article text), `"html"` (raw HTML), or `"full"` (all capture fields)
- **archive**: If true, create Source node and save files (default false)
- **spn**: Queue for Wayback Machine SPN on success (default true)
- **tags**: Tags for Source node if archive=true (list or comma-separated string)
- **min_text_size**: Minimum chars for successful extraction (default from lib/capture)
- **timeout**: Per-tier timeout in seconds (default 15)
- **stealth**: Use anti-detection nodriver browser (default false)
- **extract**: Extract mode for stealth -- `"text"`, `"html"`, `"links"`, `"all"` (default `"text"`)
- **js_eval**: JavaScript to evaluate on page (stealth mode only)
- **bypass_cache**: Skip response cache (stealth mode only)
- **cache_ttl**: Cache TTL in seconds (stealth mode, default 3600)
- **min_delay**: Override domain rate limit delay in seconds (stealth mode)
- **max_retries**: Max retry attempts for stealth mode (default 3)

**Examples:**
```json
// Read a page (default four-tier pipeline)
research("read", {"url": "https://example.com/article"})

// Read and archive in one step
research("read", {"url": "https://example.com/article", "archive": true, "tags": ["research"]})

// Read a bot-protected site with stealth mode
research("read", {"url": "https://protected-site.com", "stealth": true})

// Extract links from a page
research("read", {"url": "https://example.com", "stealth": true, "extract": "links"})
```

**Returns:** `{success, url, domain, title, content, text_size, word_count, capture_method, metadata, tier_errors, archive_status, spn_queued}`

## archive

**Primary archiving tool.** Archives a URL with Source node creation in both databases. Replaces `archive_source` and `save_page` with a single intent-based operation.

Three modes:
- **auto** (default): Four-tier capture pipeline. Most reliable, handles 90%+ of URLs.
- **mhtml**: CDP Page.captureSnapshot for full MHTML archive. Visual fidelity.
- **full**: Nodriver capture with raw HTML preservation. Handles PDF URLs.

**Parameters:**
- **url** (required): URL to archive
- **mode**: `"auto"` (default), `"mhtml"`, or `"full"`
- **tags**: Tags for Source node (list or comma-separated string)
- **entry_id**: Lifestream entry ID for CITES edge wiring
- **context**: Reason/context for archiving
- **wait_seconds**: JS render wait (mode-dependent defaults: auto=n/a, mhtml=8, full=5)
- **spn**: Queue for Wayback Machine SPN on success (default true)
- **timeout**: Per-operation timeout in seconds
- **min_text_size**: Minimum chars for successful extraction (auto mode only)

**Examples:**
```json
// Archive with default auto mode
research("archive", {"url": "https://example.com/article", "tags": ["evidence"]})

// Archive with MHTML for visual fidelity
research("archive", {"url": "https://example.com/page", "mode": "mhtml"})

// Archive and wire to a lifestream entry
research("archive", {"url": "https://example.com/source", "entry_id": "ls-20260304-001"})
```

**Returns:** `{success, url, domain, title, mode, capture_method, text_size, archive_paths, source_node, cites_edge, spn_queued}`

---

## extract_saved_article

Extract article text from a browser "Save Page Complete" download (HTML + `_files/` folder), stripping client-side JavaScript paywall enforcement. Many paywalled sites serve full content and enforce the paywall with JS. (Renamed from `extract_article` — old name still works.)

**Parameters:**
- **path** (required): Path to HTML file or folder containing saved page
- **url**: Original article URL (auto-detected from HTML if omitted)
- **entry_id**: Optional lifestream entry ID for CITES edge
- **context**: Why this source matters
- **tags**: JSON array of tags for Source node

## search_pdf

Search and extract text from PDF documents. Downloads PDFs from URLs or reads local files, extracts metadata, searches for terms, and optionally archives as a Source node. Use this instead of `read` for PDF URLs.

**Parameters:**
- **path** (required): Local file path or URL to PDF
- **search_terms**: Comma-separated search terms (e.g. `"Frantzve,IMET"`)
- **extract_pages**: Page range to extract full text (e.g. `"1-5,307"`)
- **archive**: If true, create Source node in Neo4j (default false)
- **context_chars**: Characters of context around each match (default 300)

## queue_archive

Queue a URL for async archiving. Instant and non-blocking -- adds the URL to the processing queue. Use `process_queue` to process queued items.

**Parameters:**
- **url** (required): The URL to archive
- **priority**: `"high"`, `"normal"`, or `"low"` (default: `"normal"`)
- **entry_id**: Optional lifestream entry to wire CITES edge
- **context**: Context note for the archive
- **tags**: Tags for the Source node
- **wait_seconds**: Page load wait time for the capture worker (default 5)

## process_queue

Process queued URLs with the four-tier capture pipeline. Creates Source nodes atomically for each successful capture.

**Parameters:**
- **batch_size**: Max items to process per call (default 5)
- **skip_failed**: Skip items that have failed before (default false)
- **retry_failed**: Re-attempt items with status `"failed"` (default false)
- **min_delay**: Minimum seconds between same-domain requests (default 3)

**Example:**
```json
// Process next 5 queued items
research("process_queue")

// Retry previously failed items
research("process_queue", {"retry_failed": true, "batch_size": 3})
```

**Returns:** `{processed, succeeded, failed, results: [{url, status, source_node_id, text_length, error}]}`

## check_queue

Check archive queue status. Returns summary of queued, processing, completed, and failed items.

**Parameters:**
- **queue_id**: Check a specific queue entry by ID
- **status_filter**: Filter by status: `"queued"`, `"processing"`, `"completed"`, `"failed"`, `"all"` (default: summary)

## read_staged

Read structured extraction from a completed async capture.

**Parameters:**
- **queue_id**: The queue ID of a completed capture
- **url_hash**: Alternatively, the URL hash to look up directly in staged/
- **mark_processed**: If true, update queue status to `"processed"` (default false)

## ingest_saved

Ingest user-saved HTML pages from `uploads/websites/` into Source nodes. Scans the directory for HTML files, extracts article text, and creates Source nodes.

**Parameters:**
- **file**: Specific filename in uploads/websites/ (optional -- omit to scan all)
- **url**: Original URL of the page (optional -- extracted from HTML if possible)
- **tags**: Tags for the Source node (list or comma-separated string)
- **entry_id**: Lifestream entry ID to wire CITES edge
- **spn**: Queue for Wayback SPN preservation (default true)

## wayback_lookup

Query the Wayback Machine CDX API for existing snapshots of a URL.

**Parameters:**
- **url** (required): The URL to look up
- **count**: Max snapshots to return (default 5, max 25)
- **from_date**: Optional start date filter (`YYYYMMDD` or `YYYY`)
- **to_date**: Optional end date filter (`YYYYMMDD` or `YYYY`)

## check_sources

Check if archived source URLs are still live. Compares current status against archived versions.

**Parameters:**
- **domain**: Optional domain filter (e.g. `"foxnews.com"`)
- **entry_id**: Optional entry ID to check only sources cited by that entry

## archive_inventory

Inventory local web archives and reconcile with Neo4j Source nodes. Scans `ClaudeFiles/archives/` for all captured web pages.

**Parameters:**
- **domain**: Optional domain filter. None = all domains.
- **reconcile**: Whether to check against Neo4j Source nodes (default true)

## search_records

Search public record databases for entity information. Queries multiple sources by name and returns structured results.

**Available sources:** SEC EDGAR (always available), SEC Company Search (always available), OpenCorporates (optional API key), CourtListener (optional API key), USPTO Patents, NHTSA, FMCSA.

**Parameters:**
- **query** (required): Search terms -- company name, person name, keywords, or VIN. Alias: `name`.
- **record_types**: Comma-separated list of sources. Options: `sec`, `sec_company`, `patents`, `opencorporates`, `courtlistener`, `nhtsa`, `fmcsa`, `all`. Default: `"all"`.
- **state**: State abbreviation for jurisdiction filtering (e.g. `"AZ"`)
- **forms**: SEC form types to filter (e.g. `"10-K,10-Q,8-K"`)
- **max_results**: Max results per source (default 10, max 25)
- **model_year**: Optional model year for NHTSA VIN decode accuracy

**Examples:**
```json
// Search all sources
research("search_records", {"query": "Turning Point USA"})

// SEC filings only
research("search_records", {"query": "Lori Frantzve", "record_types": "sec", "forms": "10-K,10-Q"})
```

## search_business

Search state business entity registries (currently UT, AZ) by name, principal, agent, or entity number.

**Parameters:**
- **query**: Entity name, principal name, or agent name to search for
- **state** (required): Two-letter state code (e.g. `"UT"`, `"AZ"`)
- **search_type**: What to search by -- `"entity_name"` (default), `"principal_name"`, `"agent_name"`, `"entity_number"`
- **entity_id**: If provided, fetch detailed entity info instead of searching
- **max_results**: Max search results to return (default 10)

## vin_decode

Decode a VIN via NHTSA -- returns make, model, year, engine, plant, and optionally safety recalls and owner complaints.

**Parameters:**
- **vin** (required): 17-character Vehicle Identification Number
- **model_year**: Optional model year for better decode accuracy
- **include_recalls**: If true, fetch safety recalls (default false)
- **include_complaints**: If true, fetch owner complaints (default false)

## generate_report

Query the knowledge graph and generate a markdown research report with entity details, timelines, evidence citations, and gap analysis.

**Parameters:**
- **topic** (required): Report title
- **entities** (required): List of seed entity names
- **depth**: Expansion depth (1-3, default 2)
- **format**: `"internal"` (full detail) or `"public"` (journalist-friendly, default: `"internal"`)
- **exclude_labels**: List of node labels to exclude
- **include_evidence**: Include source citations (default true)
- **include_timeline**: Include chronological events (default true)
- **include_gaps**: Include section on unsourced entities (default true)
- **bundle_archives**: Create a zip of report + archived source files (default false)
- **output_dir**: Override output directory
- **database**: Target database (default: `"corcoran"`)

---

## Legacy Operations (Deprecated)

These operations still work for backward compatibility but are superseded by `read` and `archive`.

- **fetch_page** -> Use `read` instead. `fetch_page` is now a thin wrapper that calls `read`.
- **browse_url** -> Use `read` with `stealth: true` instead. `browse_url` is still used internally as a subprocess target.
- **archive_source** -> Use `archive` with `mode: "full"` instead. `archive_source` is still used internally as a subprocess target.
- **save_page** -> Use `archive` with `mode: "mhtml"` instead. `save_page` is still used internally as a subprocess target.
