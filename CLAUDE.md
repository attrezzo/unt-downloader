# UNT Archive Re-OCR Pipeline — Claude Code Project

This file is the single source of truth for Claude Code working in this
project. Read it fully before touching any file. It supersedes all docstrings
for architectural decisions.

---

## Project intent

This project re-OCRs and translates archival images from the UNT Portal to
Texas History (texashistory.unt.edu) to the best standard modern technology
allows — and is explicitly designed so that standard can be raised over time.

**Three principles govern every design decision:**

1. **Re-processability.** Source images are permanent. OCR and translation
   outputs are not. Every word-level confidence score is preserved so future
   tools can re-run only the low-confidence regions without touching content
   that is already certain. When OCR technology improves, the pipeline re-runs
   selectively on the hard parts at minimal cost.

2. **Engine-agnosticism.** The OCR step currently uses ABBYY XML (legacy),
   Tesseract (two passes), Kraken, and Claude. These are interchangeable
   components. New engines slot in via the word-token interface; the alignment
   and arbitration layer handles any mix automatically. Do not couple logic to
   a specific engine.

3. **Document-type flexibility.** Built on German Fraktur newspapers but not
   specific to them. collection.json drives language, script type, layout
   assumptions, and contextual hints. The architecture must support printed
   text, handwritten documents, and photographic imagery by adjusting config
   and swapping preprocessing/layout stages — not by rewriting the pipeline.

The primary current collection is the **Bellville Wochenblatt** (1891,
LCCN sn86088292), 118 issues, 8 pages each, German Fraktur on 35mm microfilm.
It is the reference implementation, not the only target.

---

## Confidence tracking — the core architectural commitment

Every OCR result carries a numeric confidence score (0–100). The alignment
step classifies each word position:

- **HIGH-CONFIDENCE**: all present sources agree AND min score ≥ TESS_CONF_MIN
  (currently 40). Written verbatim to agreed text. Does not reach Claude.
- **LOW-CONFIDENCE**: any source disagrees, or any score below threshold.
  Written as `{?provisional?}` in agreed text. Added to dispute table with
  all source readings, scores, and surrounding line context.

**The planned `--reprocess-low-confidence` mode** will skip HIGH-CONFIDENCE
regions and re-run only disputed positions through newer engines or models.
Never discard per-word confidence scores. Never flatten to a boolean before
storing. This data is the mechanism for future selective improvement.

`[unleserlich]` is zero-confidence: examined by all tools, judged unreadable.
Future models can target these positions specifically.

---

## File map

```
unt_archive_downloader.py   Main entry point. Configuration, discovery,
                            download, and pipeline orchestration. Calls
                            the other scripts as subprocesses via run_worker().
unt_ocr_correct.py          Multi-engine OCR + article segmentation.
                            The OCR engine layer is the primary extension point.
unt_translate.py            Translates corrected/ text to English.
                            Smart --resume repairs partial files in place.
unt_render_pdf.py           Renders translated/ text as newspaper-style PDFs.
                            No Claude API calls — ReportLab only.
unt_cost_estimate.py        Interactive model selector + cost confirmation.
                            Called by ocr_correct and translate before API use.
claude_rate_limiter.py      Thread-safe dual token-bucket rate limiter.
pricing.json                Hand-maintained pricing table. Edit when prices change.
ocr_pipeline/               Batch-aware OCR preprocessing. Extracts style
                            signatures, confidence data, and low-confidence
                            region maps across issues. Feeds calibrated
                            parameters into unt_ocr_correct.py preprocessing.
                            Run: python -m ocr_pipeline --config-path collection.json
CLAUDE.md                   This file.
README.md                   End-user quickstart (shorter, less technical).
```

---

## Collection directory layout

Every collection is self-contained. All paths are relative to the collection
root (e.g. `bellville_wochenblatt/`).

```
collection.json
metadata/
    all_issues.json             [{ark_id, date, volume, number, pages, full_title, ...}]
ocr/
    {ark}_vol{v}_no{n}_{date}.txt    Raw portal HTML (preserved as-is)
images/
    preload_failures.json            Pages that failed image download
    {ark_id}/
        page_01.jpg
        page_02.jpg
        ...
abbyy/
    README.txt                  Auto-created with request instructions
    {ark}_vol{v}_no{n}_{date}.xml    Optional ABBYY FineReader XML
corrected/
    correction_log.json
    {ark}_vol{v}_no{n}_{date}.txt    Best-available OCR in source language
articles/
    {ark_id}/
        pg01_art001.txt
        pg01_art002.txt
        ...
        manifest.json
translated/
    translation_log.json
    {ark}_vol{v}_no{n}_{date}.txt    English translations
confidence/
    {ark_id}_page{NN}.json      Per-word confidence records (0–100 integers)
artifacts/
    pipeline_log.jsonl          Batch pipeline structured log
    batch_summary.json          Aggregate statistics from latest sweep
    style_signatures.json       Per-issue typography/degradation profiles
    low_confidence/
        {ark_id}_page{NN}.json  Regions flagged for Pass B refinement
    debug/                      Debug images (when --save-debug-images)
pdf/
    {ark}_vol{v}_no{n}_{date}.pdf
```

Issue filenames are always: `{ark_id}_vol{vv}_no{nn}_{date}.txt`
Example: `metapth1478562_vol01_no01_1891-09-17.txt`

---

## Shared text file format

**Every file in ocr/, corrected/, and translated/ uses this exact format.**
Both parsers (`parse_ocr_pages()` in unt_ocr_correct.py and `parse_pages()`
in unt_translate.py) depend on this structure. Never change it.

```
=== COLLECTION TITLE ===
ARK:    metapth1478562
URL:    https://texashistory.unt.edu/ark:/67531/metapth1478562/
Date:   1891-09-17
Volume: 1   Number: 1
Title:  Bellville Wochenblatt. (Bellville, Tex.), Vol. 1, No. 1 ...
============================================================

--- Page 1 of 8 ---
[text content]

--- Page 2 of 8 ---
[text content]
```

**Separator line**: exactly 60 `=` characters.
**Header detection**: `line.startswith('=' * 10)` — anything with 10+ `=` ends the header.
**Page marker regex**: `^--- Page (\d+) of (\d+) ---$`
**Encoding**: UTF-8 throughout. All file writes use `encoding="utf-8"`.

---

## Article file format

Written by `write_article_files()`. One file per discrete article or
advertisement. Never write to `articles/` through any other path.

Filename: `pg{NN}_art{NNN}.txt` (zero-padded, sorts correctly).

```
ARK:     metapth1478562
ISSUE:   Bellville Wochenblatt Vol. 1 No. 1, 1891-09-17
PAGE:    3  (of 8)
SPANS:   3-4              ← ONLY present when article crosses page boundary
TYPE:    article          ← article | advertisement | masthead | notice | poetry

Headline text here

Body text. [unleserlich] for unreadable words.
Compound words rejoined. Column markers removed.
```

SPANS line is omitted when pg_first == pg_last.
TYPE comes from Claude's segmentation call; "article" is the default.

`manifest.json` in each `articles/{ark_id}/` directory:
```json
{
  "ark_id": "metapth1478562",
  "issue": "Bellville Wochenblatt ...",
  "articles": [
    {
      "file":     "pg01_art001.txt",
      "page":     1,
      "spans":    "1",
      "type":     "masthead",
      "headline": "Bellville Wochenblatt."
    }
  ]
}
```

---

## Pipeline commands

```bash
# Full pipeline, one collection
python unt_archive_downloader.py --configure           # interactive setup, once
python unt_archive_downloader.py --discover            # find all issue ARKs
python unt_archive_downloader.py --download-ocr        # fetch raw portal HTML
python unt_archive_downloader.py --preload-images      # cache IIIF page scans
python unt_archive_downloader.py --correct --resume    # OCR correction + segmentation
python unt_archive_downloader.py --translate --resume  # translate to English
python unt_archive_downloader.py --render-pdf --resume # produce PDFs
python unt_archive_downloader.py --status              # progress report

# Common flags (work on most steps)
--ark metapth1478562          # single issue only
--date-from 1891-09-01        # date range start
--date-to   1891-12-31        # date range end
--resume                      # skip completed work
--api-workers 3               # parallel Claude API calls (default 3)
--tier default|build|custom   # rate limit tier
--serial                      # one issue at a time (safe fallback)

# Correction-specific
--retry-failed                # retry pages marked [CORRECTION FAILED]
--workers 4                   # image download threads (default 4)
--delay 2.0                   # seconds between page API calls

# Translation-specific
--max-output-tokens 32000     # Claude output budget (default 32000, max 64000)

# PDF-specific
--columns 5                   # columns per page (default 5)

# Direct script invocation (bypasses orchestrator)
python unt_ocr_correct.py --config-path collection.json --resume
python unt_translate.py   --config-path collection.json --resume
python unt_render_pdf.py  --config-path collection.json

# Multi-collection (from any directory)
python unt_archive_downloader.py --config-dir bellville_wochenblatt --status
python unt_archive_downloader.py --config-dir neu_braunfelser_zeitung --correct --resume
```

Orchestrator passes flags through to worker scripts via `run_worker()`,
which calls the scripts as subprocesses using `subprocess.run()`.

---

## collection.json — complete schema

```json
{
  "title_name":         "Bellville Wochenblatt",
  "lccn":               "sn86088292",
  "publisher":          "C.A. Hermes",
  "pub_location":       "Bellville, Texas",
  "date_range":         "1891-1892",
  "language":           "German",
  "typeface":           "Fraktur",
  "source_medium":      "35mm microfilm",
  "layout_type":        "newspaper",
  "permalink":          "https://texashistory.unt.edu/explore/titles/.../",
  "title_keyword":      "bellville",
  "community_desc":     "German immigrant community, Austin County, Texas",
  "place_names":        "Bellville, Austin County, San Antonio, New Braunfels",
  "organizations":      "Turnverein, Schützenverein, Austin County Historical Society",
  "historical_context": "German-Texan settlement, Reconstruction era, 1891 economy",
  "subject_notes":      "German Day celebrations, Austin County court news, farm prices",
  "anthropic_api_key":  "sk-ant-...",
  "claude_model":       "claude-sonnet-4-6"
}
```

**Field notes:**
- `typeface` controls whether the Fraktur error substitution table is included
  in correction prompts. Set to anything other than "Fraktur" (case-insensitive)
  to omit it.
- `language` drives Tesseract language model selection and translation prompts.
- `title_keyword` is used by --discover to filter IIIF manifests by title.
- `layout_type` is informational now; will drive Stage 3 and Stage 9 dispatch
  when multiple layout profiles are implemented. Planned values: `newspaper`,
  `letter`, `ledger`, `photograph`, `handwritten_document`.
- `claude_model` overrides the `CLAUDE_MODEL` constant at startup.
- `anthropic_api_key` is the fallback if `ANTHROPIC_API_KEY` env var is unset.

---

## Key constants

**unt_ocr_correct.py:**
```python
ILLEGIBLE        = "[unleserlich]"          # THE canonical unintelligible marker
TESS_CONF_MIN    = 40                       # below this = disputed regardless of agreement
TESS_PSM_A       = "--psm 6 --oem 1 --dpi 300"   # uniform text block
TESS_PSM_B       = "--psm 4 --oem 1 --dpi 300"   # single column of text
TESS_LANG_PRIORITY = ["deu_frak+deu", "deu_frak", "deu", "eng"]
CLAUDE_MODEL     = "claude-sonnet-4-6"      # overridden by collection.json
```

**unt_translate.py:**
```python
BUDGET_EXCEEDED_PREFIX    = "[BUDGET EXCEEDED: PAGE "
FAILED_PREFIX             = "[TRANSLATION FAILED: PAGE "
NO_SOURCE_PREFIX          = "[NO SOURCE TEXT: PAGE "
DEFAULT_MAX_OUTPUT_TOKENS = 32_000
```

**claude_rate_limiter.py:**
```python
TIER_DEFAULT = {"rpm": 50,    "tpm": 40_000}   # free / low usage
TIER_BUILD   = {"rpm": 1_000, "tpm": 80_000}   # after $5 spend
TIER_CUSTOM  = {"rpm": 50,    "tpm": 40_000}   # edit to match your limits
```

**pricing.json** — models and $/MTok rates. Update when Anthropic changes pricing.
Check: https://platform.claude.com/docs/en/about-claude/pricing
The `_meta.last_verified` field shows when prices were last confirmed.

---

## Word token interface — the OCR engine contract

Every OCR engine must return a list of dicts in this format. The alignment
stage accepts any list of these dicts regardless of source.

```python
{
    "text":   str,   # OCR reading. Use ILLEGIBLE constant if unreadable.
    "conf":   int,   # 0–100. Below TESS_CONF_MIN = always disputed.
                     # Use 0 for ILLEGIBLE words.
    "source": str,   # Engine label: "abbyy"|"tess_a"|"tess_b"|"kraken"|custom
    "left":   int,   # Bounding box in COLUMN-LOCAL pixel coordinates.
    "top":    int,   # x=0 is the left edge of the column strip, not the page.
    "right":  int,   # ABBYY tokens are remapped from page coords to column-local
    "bottom": int,   # in process_page() before passing to align_sources().
}
```

**To add a new OCR engine:**
1. Write a function `my_engine_tokens(col_img, source_tag) -> list` that
   returns word tokens in the format above.
2. Add a `HAS_MY_ENGINE` boolean at the top of unt_ocr_correct.py using
   the same try/except import pattern as HAS_KRAKEN.
3. In `process_page()`, add:
   ```python
   if HAS_MY_ENGINE:
       my_toks = my_engine_tokens(strip, "my_engine")
       if my_toks:
           sources["my_engine"] = my_toks
           engines_used.add("my_engine")
   ```
4. No other changes needed. The alignment and arbitration stages are automatic.

---

## Function signatures — unt_ocr_correct.py

```python
# ── Full page pipeline ────────────────────────────────────────────────────
def process_page(
    ark_id: str,
    page_num: int,
    total_pages: int,
    unt_ocr_text: str,          # HTML-stripped UNT portal OCR (fallback only)
    issue_meta: dict,           # one entry from all_issues.json
    api_key: str,
    correction_prompt: str,     # built by build_correction_prompt(config)
    tess_lang: str | None,      # from detect_tesseract_lang()
    abbyy_page_tokens: list,    # from parse_abbyy_page(), may be []
    abbyy_blocks: list,         # from parse_abbyy_page(), may be []
    worker_id: str = "",
    rate_limiter=None,
) -> tuple:                     # (corrected_text: str, pipeline_summary: str)

# ── ABBYY XML ────────────────────────────────────────────────────────────
def parse_abbyy_page(
    xml_path: Path,
    page_index: int = 0,        # 0-indexed page within the XML file
) -> tuple:                     # (tokens: list, blocks: list)
                                # blocks = [{type, left, top, right, bottom}]

def abbyy_xml_path(issue_fname: str) -> Path | None:
    # Returns ABBYY_DIR/{stem}.xml if it exists and has content, else None

def abbyy_column_boundaries(block_bounds: list) -> list:
    # Returns sorted list of x-positions derived from ABBYY block edges

# ── Preprocessing ────────────────────────────────────────────────────────
def preprocess_image(img_gray) -> np.ndarray:
    # CLAHE + median blur. Input/output: grayscale numpy array, same shape.

def detect_content_bounds(img_gray) -> tuple:
    # Returns (left, top, right, bottom) trimming dark microfilm borders.

# ── Column detection (always from image) ─────────────────────────────────
def detect_columns_from_image(
    img_gray,
    content_bounds: tuple,
    expected_cols: int = 5,
) -> list:                      # [(x_start, x_end), ...]

# ── Boundary comparison ───────────────────────────────────────────────────
def compare_boundaries(
    opencv_cols: list,          # from detect_columns_from_image()
    abbyy_gutters: list,        # from abbyy_column_boundaries()
    snap_tolerance: int = 20,   # px — OpenCV snaps to ABBYY within this distance
    expected_cols: int = 5,     # controls Pass 3 pruning of excess gutters
) -> tuple:                     # (final_cols: list, report: str)
# Three-pass reconciliation:
#   Pass 1: snap agreeing OpenCV gutters to ABBYY positions
#   Pass 2: insert ABBYY-only gutters that OpenCV missed
#   Pass 3: prune excess OpenCV-only gutters ranked by distance from ABBYY

# ── OCR engines ──────────────────────────────────────────────────────────
def detect_tesseract_lang() -> str | None:
    # Returns best available lang string, e.g. "deu_frak+deu"

def tesseract_tokens(
    col_img,                    # numpy array, column-local coords
    lang: str,
    config: str,                # e.g. TESS_PSM_A or TESS_PSM_B
    source_tag: str,            # "tess_a" or "tess_b"
) -> list:                      # word tokens; [] on any error

def kraken_tokens(
    col_img,
    source_tag: str = "kraken",
) -> list:                      # word tokens; [] if not installed or error

# ── Word alignment ────────────────────────────────────────────────────────
def align_sources(
    sources: dict,              # {"abbyy": [tok,...], "tess_a": [tok,...], ...}
    pos_tolerance: int = 15,    # px — tokens within 3× this = same word position
) -> list:
# Returns list of AlignedWord dicts:
# {
#   "tokens":         {"abbyy": tok|None, "tess_a": tok|None, ...},
#   "agree":          bool,
#   "consensus":      str,         # best reading or ILLEGIBLE
#   "dispute_reason": str,         # empty if agree, explanation if not
# }

def split_agree_dispute(aligned: list) -> tuple:
# Returns (agreed_text_lines: list[str], dispute_table: list[dict])
# agreed_text_lines: one string per OCR line; disputed words as {?provisional?}
# dispute_table entry:
# {
#   "top":          int,
#   "left":         int,
#   "provisional":  str,
#   "readings":     {"tess_a": "word", "tess_b": "word", ...},
#   "confs":        {"tess_a": 87, "tess_b": 43, ...},
#   "reason":       str,
#   "line_context": str,    # the full line, for Claude's reference
# }

# ── Claude arbitration ────────────────────────────────────────────────────
def arbitrate_with_claude(
    ark_id: str,
    page_num: int,
    total_pages: int,
    agreed_text: str,           # agreed_text_lines joined with \n, [Column N] markers
    dispute_table: list,        # from split_agree_dispute()
    issue_meta: dict,
    api_key: str,
    correction_prompt: str,
    rate_limiter=None,
) -> str:
# Sends image + agreed text + dispute table. Claude resolves {?...?} markers.
# Safety net: re.sub(r'\{\?([^?}]*)\?\}', r'\1', result) cleans leftovers.

# ── Segmentation and stitching ────────────────────────────────────────────
def segment_page(
    page_num: int,
    corrected_text: str,
    api_key: str,
    rate_limiter=None,
) -> list:
# Text-only Claude call. Returns list of item dicts:
# {
#   "type":                str,   # article|advertisement|masthead|notice|poetry
#   "headline":            str,
#   "body":                str,
#   "continues_from_prev": bool,
#   "continues_to_next":   bool,
#   "page":                int,
#   "page_span":           [first_pg, last_pg],
# }

def stitch_boundary(last_item: dict, first_item: dict,
                    api_key: str, rate_limiter=None) -> str:
# Returns "merge" or "separate". Fast-path heuristics before API call.

def stitch_all_pages(all_items: list, api_key: str,
                     rate_limiter=None, worker_id: str = "") -> list:
# Iterates page boundaries in sorted order. Sequential — do not parallelize.

def write_article_files(issue: dict, all_items: list, ark_dir: Path) -> int:
# Writes pg{NN}_art{NNN}.txt and manifest.json. Clears existing files first.
# Returns count of files written.
# Only function that writes to articles/. Use exclusively.

# ── Image fetching ────────────────────────────────────────────────────────
def fetch_page_image(ark_id: str, page: int) -> tuple:
# Returns (bytes, "image/jpeg") from cache or UNT IIIF. (None, None) on failure.

def is_valid_cached_image(path: Path) -> bool:
# True if file exists AND size >= 50,000 bytes (real scan, not an error stub).

# ── OCR file parsing ──────────────────────────────────────────────────────
def strip_ocr_html(text: str) -> str:
# Strips UNT portal HTML to plain OCR text. Safe on already-plain text (no-op).

def parse_ocr_pages(text: str) -> tuple:
# Returns (header: str, pages: {page_num: content_str})

# ── Shared API call ───────────────────────────────────────────────────────
def claude_api_call(
    payload: dict,
    api_key: str,
    rate_limiter=None,
    est_tokens: int = 8000,
) -> str:
# Retries 3× with exponential backoff on 429/503/529. Raises on other errors.
# Returns text content of first text block in response.

# ── System prompt ─────────────────────────────────────────────────────────
def build_correction_prompt(config: dict) -> str:
# Builds system prompt from collection.json. Includes Fraktur error table
# only if config["typeface"] contains "fraktur" (case-insensitive).
# This is a document-type-specific function. Write a new one for other types.
```

---

## Function signatures — unt_translate.py

```python
def audit_translated_file(trans_path: Path) -> dict:
# Reads existing translated file, classifies each page.
# Returns:
# {
#   "status":            "complete"|"partial"|"empty"|"absent",
#   "header":            str,
#   "total_pages":       int,
#   "pages":             {pg_num: content_str},
#   "needs_translation": [pg_num, ...],
# }

def _is_untranslated_content(page_text: str) -> bool:
# True for: raw HTML, BUDGET_EXCEEDED marker, TRANSLATION FAILED marker,
#           NO SOURCE TEXT marker, TRANSLATION MISSING marker, ERROR marker,
#           empty string. Also True if BUDGET_EXCEEDED appears mid-content.

def get_source_pages(issue: dict, pages_needed: list) -> tuple:
# Returns (source_pages: {pg_num: plain_text}, using_corrected: bool)
# Priority: corrected/ → strip HTML from ocr/ → empty string
# Never returns HTML. Always plain text.

def write_translated_file(
    path: Path,
    header: str,
    pages: dict,                # {pg_num: translated_text} — ALL pages merged
    model: str,
    using_corrected: bool,
    timestamp: str = None,
) -> None:
# THE ONLY function that writes to translated/. Always receives full merged dict.

def call_claude(
    pages_to_translate: dict,   # {pg_num: corrected_ocr_text}
    issue: dict,
    total_pages: int,
    api_key: str,
    system_prompt: str,
    max_output_tokens: int,
    rate_limiter=None,
) -> dict:
# One Claude call for multiple pages. Returns {pg_num: translated_text}.
# Handles stop_reason="max_tokens" by writing BUDGET_EXCEEDED markers with
# embedded corrected OCR for retry.

def parse_pages(text: str) -> tuple:
# Same logic as parse_ocr_pages() but in unt_translate.py.
# Returns (header: str, pages: {pg_num: content_str})
```

---

## Function signatures — unt_cost_estimate.py

```python
def choose_model_and_confirm(
    api_key: str,
    pages_to_process: int,
    step_name: str,             # "OCR correction" or "Translation"
    input_tok_per_page: int,    # estimated input tokens per page
    output_tok_per_page: int,   # estimated output tokens per page
    default_model: str = "claude-sonnet-4-6",
) -> str:
# Interactive: shows live model list with pricing, cost estimate, y/N prompt.
# Returns chosen model ID. Calls sys.exit(0) if user declines.
# Fetches live model list from /v1/models API.
# Pricing from pricing.json (human-maintained — update when Anthropic changes rates).

def load_pricing() -> dict:
# Returns {model_id: {input, output, tier, note}} from pricing.json

def pricing_meta() -> dict:
# Returns _meta block (source, last_verified, note, units)
```

---

## Function signatures — claude_rate_limiter.py

```python
class ClaudeRateLimiter:
    def __init__(self, rpm: int = 50, tpm: int = 40_000,
                 safety_factor: float = 0.80): ...
    # safety_factor: targets this fraction of stated limits to avoid hitting them

    def acquire(self, estimated_tokens: int = 2000):
    # Blocks until both RPM and TPM slots are available.
    # Call immediately before each Claude API request.
    # Over-estimate tokens if unsure — better to wait than exceed limit.

    def record_usage(self, input_tokens: int = 0, output_tokens: int = 0):
    # Updates statistics. Does NOT deduct from buckets (already done at acquire).
    # Call after each successful API response with actual usage from response body.

    def status_line(self) -> str:
    # Single-line status for progress output.

def limiter_from_tier(tier_name: str) -> ClaudeRateLimiter:
# "default" → rpm=50,  tpm=40_000
# "build"   → rpm=1000, tpm=80_000
# "custom"  → edit TIER_CUSTOM in claude_rate_limiter.py
# Check your actual limits: console.anthropic.com/settings/limits
```

---

## OCR correction pipeline — internal flow

`process_page()` runs these stages in sequence. Each is a swap point.

**Stage 1 — ABBYY XML (called before process_page in process_issue)**
`parse_abbyy_page(xml_path, page_index)` parses one page from the XML file.
`page_index = page_num - 1` (0-indexed). Returns (tokens, blocks).
Called once per page in `process_issue()` and passed into `process_page()`.
If xml_path is None or page has no data, both return as [].

**Stage 2 — Image loading and preprocessing**
`fetch_page_image()` returns cached JPEG bytes or fetches from UNT IIIF.
`cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)` converts to numpy array.
`preprocess_image()` applies CLAHE (clipLimit=2.5, tileGridSize=(8,8))
then medianBlur(3). Returns enhanced array, same dtype and shape.
Swap point: add deskew, Sauvola thresholding, or other steps as needed.

**Stage 3 — Layout detection (always from image)**
`detect_content_bounds()` thresholds at pixel value 60 to find content
versus dark microfilm border. Returns (left, top, right, bot).
`detect_columns_from_image()` analyzes the bottom 30% of content area
(zone: top + height * 7//10 to top + height * 95//100) using vertical
dark-pixel projection, uniform_filter1d(size=12), then find_peaks with
distance=80 and prominence=5%. Selects top N-1 valleys by prominence.
Swap point: replace with Kraken BLLA for handwritten text, ruling detection
for ledgers, etc.

**Stage 4 — Boundary comparison (only when abbyy_blocks is non-empty)**
`abbyy_column_boundaries()` clusters block left edges to derive gutter
x-positions from ABBYY block data (Text blocks + Separator blocks).
`compare_boundaries()` runs three passes:
- Pass 1: For each OpenCV gutter, snap to ABBYY if within snap_tolerance.
  Mark unmatched OpenCV gutters as "opencv_only".
- Pass 2: Insert any ABBYY gutters not matched by OpenCV. These are real
  boundaries the image analysis missed (ABBYY layout analysis is structural).
- Pass 3: If total gutters > expected_cols-1, remove excess opencv_only
  gutters in order of distance from nearest ABBYY gutter (farthest = most
  spurious). Stop when count == expected_cols-1.
Returns (final_cols, report_string). Report logged if non-trivial.

**Stage 5 — Tesseract (two passes)**
`detect_tesseract_lang()` tries TESS_LANG_PRIORITY in order and returns
the first string where all components are in pytesseract.get_languages().
Both passes use `image_to_data` (not `image_to_string`) to get per-word
confidence and bounding boxes. Words with conf < 0 are filtered. Words
with conf < 10 become ILLEGIBLE tokens with conf=0.

**Stage 6 — Kraken (optional)**
`_KRAKEN_MODEL` is a module-level None, lazy-loaded on first use.
kraken_tokens() returns [] silently on any error.

**Stage 7 — Word alignment**
`align_sources(sources)` uses anchor priority: abbyy > tess_a > tess_b > kraken.
Aligns all others to anchor by center-point distance. Tolerance is
pos_tolerance * 3 (default 45px). For each position:
- Gather all non-None readings
- Unique readings (case-insensitive) count
- If 1 unique reading AND min_conf >= TESS_CONF_MIN → agree=True
- Otherwise → agree=False, pick highest-conf reading as provisional
`split_agree_dispute()` groups by top-coordinate proximity (within 12px =
same line). Agreed words written as-is. Disputed words as `{?provisional?}`.
Dispute table entries include `line_context` (the full line string).

**Stage 8 — Claude arbitration**
`arbitrate_with_claude()` builds content list: [image_block, text_block].
Dispute table capped at 300 entries. Each row shows `{?provisional?}` then
all source readings with confidence.
Claude returns JSON: `{"corrected_text": "...", "resolutions": {...}}`.
JSON parse failure → treat whole response as corrected_text.
Safety net: `re.sub(r'\{\?([^?}]*)\?\}', r'\1', result)` strips any
remaining `{?...?}` to provisional text before returning.

**Stages 9 & 10 — Segmentation and stitching**
Run in `process_issue()` after all pages corrected.
`segment_page()` uses SEGMENTATION_PROMPT (defined in unt_ocr_correct.py).
`stitch_all_pages()` is sequential by design — do not parallelize.
Fast-path heuristics in `stitch_boundary()` avoid API calls when the answer
is obvious (first_item has headline → separate; both flagged continues → merge).

---

## Translation pipeline — audit and resume logic

`audit_translated_file()` reads existing file, calls `_is_untranslated_content()`
on each page. Returns `status="partial"` if any pages need work.

A page is untranslated if its content starts with OR contains any of:
- `[BUDGET EXCEEDED: PAGE ` (also detected mid-content after partial translation)
- `[TRANSLATION FAILED: PAGE `
- `[NO SOURCE TEXT: PAGE `
- `[TRANSLATION MISSING`
- `[TRANSLATION FAILED`
- `[ERROR:`
- `<!DOCTYPE` or `<html` (raw HTML)
- Empty string

`process_issue()` in unt_translate.py:
1. Calls `audit_translated_file()` — if complete and --resume, skip entirely.
2. For partial files: calls `get_source_pages()` for only the needed pages.
3. Calls `call_claude()` with only the untranslated pages.
4. Merges results with existing good pages dict.
5. Calls `write_translated_file()` with the full merged dict.

`call_claude()` sends all pages of an issue in one API call (cross-page
context improves terminology consistency). `_parse_response()` handles
Claude hitting max_tokens:
- The page Claude was writing when cut off gets BUDGET_EXCEEDED marker
  with embedded corrected OCR below it.
- Pages Claude never reached also get BUDGET_EXCEEDED with embedded OCR.
- On next --resume, both are detected by `_is_untranslated_content()` and
  re-sent to Claude.

---

## Downloader internals

`run_worker(script_name, extra_args)` calls scripts as subprocesses:
```python
subprocess.run([sys.executable, script_path] + extra_args, check=True)
```

`show_status()` reads `metadata/all_issues.json` and counts files in each
output directory. Image count checks for individual `page_NN.jpg` files.

`discover_issues()` scans ARK range by fetching IIIF manifests, filters by
`config["title_keyword"]`, and writes `metadata/all_issues.json`.

`_fetch_one_ocr_page()` fetches one page's HTML from the UNT `/ocr/` endpoint.
Stores raw HTML (including full portal page) to `ocr/`. Stripping happens later.

`probe_ark()` tests a single ARK ID against the title keyword to validate it.

`find_config_path()` walks up from CWD looking for `collection.json`, or
checks immediate subdirectories, before prompting to configure.

---

## Render PDF internals

`unt_render_pdf.py` uses ReportLab only — no Claude API calls.
Makes no network requests. Reads from `translated/` and optionally `images/`
for page scan fallback images when translation failed.

Key functions:
- `parse_file(path)` — parses translated .txt into page/block structure
- `render_issue_pdf(parsed, out_path, ...)` — renders one issue as PDF
- `render_standalone(translated_dir, images_dir, out_dir, ...)` — batch render
- `make_styles()` — builds ReportLab paragraph style registry
- `to_flowables(blocks, styles, col_w)` — converts text blocks to RL Flowables
- `classify(line)` — classifies each text line as headline/dateline/body/etc.
- Failed pages (raw HTML present): embed original scan image as fallback

---

## Adding a new document type

1. Create `collection.json` with appropriate `language`, `typeface`,
   `source_medium`, and `layout_type` values.

2. If preprocessing needs to change (Stage 2): add a new preprocessing
   function alongside `preprocess_image()`. Dispatch in `process_page()`
   based on `config["layout_type"]` read from collection.json.

3. Replace Stage 3 layout detection for the document type by adding a
   conditional in `process_page()`:
   - Handwritten text → Kraken BLLA baseline segmentation
   - Ledgers/tables → horizontal ruling detection
   - Photographs → caption region extraction
   - Different column counts → pass different `expected_cols`

4. Write new system prompt functions (do not extend existing with conditionals):
   - Replacement for `build_correction_prompt()` for the new type
   - New segmentation prompt replacing `SEGMENTATION_PROMPT`
   - The Fraktur error table is newspaper-specific

5. Update `SEGMENTATION_PROMPT` item types for the document kind, or write
   a new one (letters, records, captions, entries, etc.).

The orchestrator `unt_archive_downloader.py` and `unt_translate.py` need
no changes for new document types.

---

## Dependency installation

```bash
# Core
pip install requests pytesseract opencv-python-headless pillow scipy numpy reportlab

# Tesseract engine
apt-get install tesseract-ocr

# German Fraktur models (Wochenblatt and similar collections)
apt-get install tesseract-ocr-deu
# Better accuracy — frak2021 from tessdata_best:
wget https://github.com/tesseract-ocr/tessdata_best/raw/main/deu.traineddata
cp deu.traineddata /usr/share/tesseract-ocr/5/tessdata/

# If Tesseract fails with "cannot find tessdata":
export TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
# Or in code before importing pytesseract:
# os.environ['TESSDATA_PREFIX'] = '/usr/share/tesseract-ocr/5/tessdata'

# Kraken — optional third OCR engine, strong for handwritten text
pip install kraken
kraken models download 10.0.0

# ABBYY XML — optional, request from UNT
# ana.krahmer@unt.edu (UNT Digital Projects Unit)
# Place in {collection}/abbyy/{issue_name}.xml
# abbyy/ directory with README is created automatically on first run
```

---

## Coding rules

**Never discard per-word confidence data.**
Confidence scores are the mechanism for future selective re-processing.
Keep raw numeric values. TESS_CONF_MIN=40 is tuneable.

**Never break --resume.**
Every step is resumable and idempotent. If you change output file formats
or naming, update the resume detection in `audit_translated_file()` and
`process_issue()` in unt_ocr_correct.py simultaneously.

**ocr/ stores raw portal HTML. Strip only at read time.**
`strip_ocr_html()` is called at the point of use in `process_issue()`.
The raw HTML is preserved for potential future layout reconstruction.
Never strip at download time in unt_archive_downloader.py.

**corrected/ is the only translation input.**
`get_source_pages()` enforces this — it reads corrected/ first, falls back
to HTML-stripped ocr/ only as a last resort for pages correction never ran.
Never pass raw HTML or ocr/ content directly to Claude translation calls.

**Translated files are patched, not rewritten.**
`write_translated_file()` is the only function that writes to translated/.
Always pass the full merged pages dict (existing good pages + new pages).

**OCR engines are independent components.**
Engine-specific logic lives only in the engine's function. `align_sources()`
does not know what sources exist. `process_page()` builds the sources dict;
everything downstream is automatic.

**compare_boundaries() always needs expected_cols.**
Do not hardcode 5. Pass it from collection.json at every call site.
Different document types will have different layout counts.

**System prompts are document-type-specific, not conditional.**
Write separate prompt builders for new document types. Adding if/else
branches to `build_correction_prompt()` for different document types
makes prompts unmaintainable and untestable.

**The {?...?} dispute marker format is a contract between three locations.**
`split_agree_dispute()` writes them.
`arbitrate_with_claude()` explains them to Claude and receives resolved text.
`re.sub(r'\{\?([^?}]*)\?\}', r'\1', result)` in arbitrate_with_claude()
cleans any that Claude fails to resolve.
Do not change this format without updating all three.

**The === separator is exactly 60 characters when written.**
Detection uses `line.startswith('=' * 10)` — the minimum, not the exact count.
Writing always uses `'=' * 60`. Both are intentional and correct.

**ILLEGIBLE = "[unleserlich]" is the only unintelligible marker.**
Used by: ABBYY parser (suspect=true words), Tesseract (conf<10 words),
Kraken (low-confidence words), Claude (unresolvable disputes, in both
correction and translation). The translated-output equivalent is "[illegible]".
Never use any other form. Never change either string.

**stitch_all_pages() is sequential by design.**
It iterates sorted page boundaries. An article spanning pages 3-4-5 merges
at 3→4, then the merged item passes 4→5. Do not parallelize this loop.

**process_page() catches all engine exceptions.**
Both `tesseract_tokens()` and `kraken_tokens()` return [] on any error and
log a warning. This is intentional — a failed engine degrades quality
without halting the pipeline. Do not change to raise.

---

## Known issues and intentional design decisions

**Masthead zone**: Page 1 of newspaper issues often has announcements spanning
multiple columns. Column detection uses the bottom 30% specifically to avoid
this. If detection fails on a specific page, verify `zone_top` clears the
masthead on that page.

**ABBYY as one vote**: ABBYY files from UNT may be 20+ years old on older
software. `compare_boundaries()` trusts ABBYY for structural completeness
(inserting gutters OpenCV missed) but defers to current image data on
position (snapping is bidirectional but pruning favors ABBYY-supported
positions). ABBYY's layout analysis is sophisticated enough to trust for
"where are columns?" even if its character recognition has since been
surpassed.

**Translation batches all pages of an issue in one call**: This gives Claude
cross-page context for terminology consistency and is more cost-efficient
than per-page calls. The tradeoff is that max_tokens exhaustion affects
multiple pages at once, hence BUDGET_EXCEEDED markers with embedded OCR.

**Kraken lazy-loads its model**: `_KRAKEN_MODEL = None` at module level,
populated on first use in `kraken_tokens()`. Loading is slow; do not force
it until actually needed.

**requests used in downloader, urllib in OCR/translate**: The downloader
uses the `requests` library (session management, headers, connection pooling
for the portal crawl). The other scripts use only stdlib `urllib.request`
to avoid the dependency. Do not add `requests` to the OCR or translate scripts.

---

## Contacts and external resources

- UNT Digital Projects Unit (ABBYY XML): ana.krahmer@unt.edu
- UNT Portal to Texas History: texashistory.unt.edu
- Anthropic rate limits: console.anthropic.com/settings/limits
- Anthropic pricing: platform.claude.com/docs/en/about-claude/pricing
- Tesseract tessdata_best: github.com/tesseract-ocr/tessdata_best
- Kraken OCR: github.com/mittagessen/kraken
- Chronicling America: chroniclingamerica.loc.gov
  (LCCN sn86088292 has bibliographic record only — no digitized pages in LoC)

**Known Texas German newspaper collections on UNT Portal:**
| Title | LCCN |
|---|---|
| Bellville Wochenblatt | sn86088292 |
| Texas Volksblatt | sn86088069 |
| Neu-Braunfelser Zeitung | sn86088194 |
| Galveston Zeitung | sn86088114 |
| Texas Staats-Zeitung | sn83045431 |
