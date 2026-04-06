#!/usr/bin/env python3
"""
UNT Archive — Claude Translator
================================
Translates corrected OCR text to English using the Claude API.
Sends ONLY the corrected OCR text — no raw OCR, no HTML, no images.

RESUME INTELLIGENCE
-------------------
--resume now understands three states for each issue file:

  1. COMPLETE    — all pages are translated English, nothing to do.
  2. PARTIAL     — some pages are translated, others contain [TRANSLATION MISSING]
                   markers or raw HTML from the old buggy run. Only the missing
                   pages are sent to Claude; the file is patched in place.
  3. ABSENT      — no translated file exists yet. Full issue is translated.

TOKEN BUDGET
------------
  --max-output-tokens  Controls Claude's max_tokens per API call.
                       Default: 32000 (covers 8 pages at ~4,000 tok/page,
                       well above the measured 2,800 tok/page average).
                       Sonnet 4.x supports up to 64,000 output tokens.
                       Increase if you see [BUDGET EXCEEDED] markers.

FALLBACK MARKERS (written when a page cannot be translated)
-----------------------------------------------------------
  [BUDGET EXCEEDED: PAGE N — partial output above, corrected OCR below]
  [TRANSLATION FAILED: PAGE N — corrected OCR below]
  [NO SOURCE TEXT: PAGE N]

  These markers are machine-readable. --resume detects them and retries
  only those pages, preserving already-translated content. This means
  a failed 8-page issue can be repaired with a 1–3 page API call rather
  than a full re-run.

USAGE
-----
  # Via the downloader (recommended):
  python unt_archive_downloader.py --translate --resume

  # Direct usage:
  python unt_translate.py --config-path bellville_wochenblatt/collection.json
  python unt_translate.py --config-path ... --resume
  python unt_translate.py --config-path ... --ark metapth1478562
  python unt_translate.py --config-path ... --max-output-tokens 48000
"""

import os, sys, json, time, re, argparse, threading
# Force line-buffered stdout so status lines appear immediately even when
# output is piped through the downloader or redirected to a log file.
sys.stdout.reconfigure(line_buffering=True)
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error

try:
    from claude_rate_limiter import ClaudeRateLimiter, limiter_from_tier
except ImportError:
    ClaudeRateLimiter = None

try:
    from unt_cost_estimate import choose_model_and_confirm
except ImportError:
    choose_model_and_confirm = None

ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL  = "claude-sonnet-4-6"   # overridden by collection.json

# Default output token budget. 32k covers 8 pages at ~4,000 tok/page with room.
DEFAULT_MAX_OUTPUT_TOKENS = 32_000

# Marker prefix written when Claude runs out of output budget mid-issue.
# Must be stable — --resume scans for it to find pages that need retrying.
BUDGET_EXCEEDED_PREFIX = "[BUDGET EXCEEDED: PAGE "
FAILED_PREFIX          = "[TRANSLATION FAILED: PAGE "
NO_SOURCE_PREFIX       = "[NO SOURCE TEXT: PAGE "

_print_lock = threading.Lock()
def tprint(*args, worker: str = "", **kwargs):
    with _print_lock:
        prefix = f"[{worker}] " if worker else ""
        import builtins
        builtins.print(f"{prefix}", *args, flush=True, **kwargs)

METADATA_DIR  = None
OCR_DIR       = None
AI_OCR_DIR    = None
TRANSLATED_DIR= None

def init_paths(collection_dir: Path):
    global METADATA_DIR, OCR_DIR, AI_OCR_DIR, TRANSLATED_DIR
    METADATA_DIR   = collection_dir / "sources" / "metadata"
    OCR_DIR        = collection_dir / "sources" / "portal_ocr"
    AI_OCR_DIR     = collection_dir / "output" / "ai_ocr"
    TRANSLATED_DIR = collection_dir / "output" / "translated"


# ---------------------------------------------------------------------------
# HTML stripping (for corrected files that may still have HTML from old runs)
# ---------------------------------------------------------------------------
def strip_html(text: str) -> str:
    """Strip UNT portal HTML down to plain OCR text. Safe on already-plain text."""
    if '<' not in text:
        return text
    m = re.search(r'id=["\']ocr-text["\'][^>]*>(.*?)</(?:div|section)',
                  text, re.S | re.I)
    inner = m.group(1) if m else text
    inner = re.sub(r'<br\s*/?>', '\n', inner, flags=re.I)
    inner = re.sub(r'<[^>]{0,500}>', ' ', inner)
    inner = inner.replace('&amp;', '&').replace('&lt;', '').replace('&gt;', '')
    inner = inner.replace('&quot;', '"').replace('&nbsp;', ' ')
    inner = re.sub(r'&#[xX][0-9a-fA-F]{1,6};', '', inner)
    inner = re.sub(r'&#\d{1,6};', '', inner)
    inner = re.sub(r'[ \t]{2,}', ' ', inner)
    inner = re.sub(r'\n{3,}', '\n\n', inner)
    return inner.strip()


def is_html(text: str) -> bool:
    """Return True if text looks like raw HTML from the UNT portal."""
    t = text.lstrip()
    return bool(re.match(r'<!DOCTYPE|<html\b', t, re.I))


# ---------------------------------------------------------------------------
# Page-level status detection
# ---------------------------------------------------------------------------
def _is_untranslated_content(page_text: str) -> bool:
    """
    Return True if this page's content is NOT a complete valid translation.
    Catches: raw HTML, all fallback markers (at start OR embedded mid-content
    after a partial translation), and empty content.
    """
    t = page_text.strip()
    if not t:
        return True
    if is_html(t):
        return True
    # Markers at start of page
    for prefix in (BUDGET_EXCEEDED_PREFIX, FAILED_PREFIX, NO_SOURCE_PREFIX,
                   '[TRANSLATION MISSING', '[TRANSLATION FAILED', '[ERROR:'):
        if t.startswith(prefix):
            return True
    # BUDGET_EXCEEDED may appear mid-content after a partial translation
    if BUDGET_EXCEEDED_PREFIX in t:
        return True
    return False


# ---------------------------------------------------------------------------
# Parse / write translated file format
# ---------------------------------------------------------------------------
PAGE_MARKER_RE = re.compile(r'^--- Page (\d+) of (\d+) ---\s*$')

def parse_pages(text: str) -> tuple[str, dict]:
    """
    Parse a translated (or OCR) file into (header_str, {page_num: content}).
    The header is everything up to and including the '==========...' separator.
    """
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    header_lines = []
    body_lines   = []
    in_header    = True
    for line in lines:
        if in_header:
            header_lines.append(line)
            if line.startswith('=' * 10):
                in_header = False
        else:
            body_lines.append(line)

    header = '\n'.join(header_lines)
    pages  = {}
    current_page  = None
    current_lines = []

    for line in body_lines:
        m = PAGE_MARKER_RE.match(line)
        if m:
            if current_page is not None:
                pages[current_page] = '\n'.join(current_lines).strip()
            current_page  = int(m.group(1))
            current_lines = []
        elif current_page is not None:
            current_lines.append(line)

    if current_page is not None:
        pages[current_page] = '\n'.join(current_lines).strip()

    return header, pages


def write_translated_file(path: Path, header: str, pages: dict,
                           model: str, using_corrected: bool,
                           timestamp: str = None) -> None:
    """Write the canonical translated file format."""
    if timestamp is None:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
    total = max(pages.keys()) if pages else 0
    lines = [
        header,
        f'[TRANSLATED TO ENGLISH — {model} — {timestamp}]',
        f'[Corrected OCR used: {"yes" if using_corrected else "no"}]',
        '',
    ]
    for pg in sorted(pages.keys()):
        lines.append(f'--- Page {pg} of {total} ---')
        lines.append(pages[pg])
        lines.append('')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines), encoding='utf-8')


# ---------------------------------------------------------------------------
# Audit an existing translated file — find pages that need work
# ---------------------------------------------------------------------------
def audit_translated_file(trans_path: Path) -> dict:
    """
    Read an existing translated file and classify each page.

    Returns:
        {
          'header': str,
          'total_pages': int,
          'pages': {pg: content},          # current content of every page
          'needs_translation': [pg, ...],   # pages that are untranslated
          'status': 'complete'|'partial'|'empty'
        }
    """
    if not trans_path.exists():
        return {'status': 'absent'}

    text = trans_path.read_bytes().decode('utf-8', errors='replace')
    header, pages = parse_pages(text)

    if not pages:
        return {'status': 'empty', 'header': header, 'pages': {}, 'needs_translation': [], 'total_pages': 0}

    total = max(pages.keys())
    needs = [pg for pg, content in sorted(pages.items()) if _is_untranslated_content(content)]

    status = 'complete' if not needs else 'partial'
    return {
        'status':            status,
        'header':            header,
        'total_pages':       total,
        'pages':             pages,
        'needs_translation': needs,
    }


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
def build_system_prompt(config: dict) -> str:
    title      = config.get('title_name', 'unknown collection')
    publisher  = config.get('publisher', 'unknown publisher')
    location   = config.get('pub_location', 'Texas')
    date_range = config.get('date_range', '')
    language   = config.get('language', 'German')
    typeface   = config.get('typeface', 'Fraktur')
    source     = config.get('source_medium', 'microfilm')
    community  = config.get('community_desc', '')
    places     = config.get('place_names', '')
    orgs       = config.get('organizations', '')
    history    = config.get('historical_context', '')
    subjects   = config.get('subject_notes', '')
    lccn       = config.get('lccn', '')
    permalink  = config.get('permalink', '')

    fraktur_note = ''
    if 'fraktur' in typeface.lower():
        fraktur_note = """
RESIDUAL OCR ERRORS — these may appear even in corrected text:
  b/d: ber=der, bie=die, unb=und  |  f/s: fein=sein, fo=so
  3/Z: 3u=Zu, 3eit=Zeit           |  cf/ck: zurücf=zurück
  «»/ß: da«=daß                    |  dropped h: sic=sich, nac=nach
"""

    community_block = f'\nCOMMUNITY:\n  {community}\n' if community else ''
    history_block   = f'\nHISTORICAL CONTEXT:\n  {history}\n' if history else ''
    subjects_block  = f'\nRECURRING SUBJECTS:\n  {subjects}\n' if subjects else ''
    places_block    = f'\nPLACE NAMES — preserve untranslated:\n  {places}\n' if places else ''
    orgs_block      = (f'\nORGANIZATION NAMES — preserve (add English gloss [in brackets] '
                       f'on first occurrence only):\n  {orgs}\n') if orgs else ''

    return f"""You are a specialist translator of historical {language}-language newspapers.

═══════════════════════════════════════════════════════════════
COLLECTION: {title}
═══════════════════════════════════════════════════════════════
Publisher  : {publisher}
Location   : {location}
Period     : {date_range}
Language   : {language}  |  Typeface: {typeface}  |  Source: {source}
{f'LCCN: {lccn}' if lccn else ''}{f'  Portal: {permalink}' if permalink else ''}
{community_block}{history_block}{subjects_block}{fraktur_note}{places_block}{orgs_block}
═══════════════════════════════════════════════════════════════
WHAT YOU RECEIVE
═══════════════════════════════════════════════════════════════
Corrected OCR text from a Claude vision pass. This is the best available
source text — translate it directly and faithfully.

═══════════════════════════════════════════════════════════════
TRANSLATION GUIDELINES
═══════════════════════════════════════════════════════════════
1. Translate to clear, natural American English appropriate to the era.
2. NEVER skip content — translate everything: news, editorials, market
   prices, legal notices, advertisements, poetry, letters, masthead.
3. Advertisements: translate product/offer; preserve advertiser name and
   address exactly; preserve ALL CAPS emphasis using **bold**.
4. Legal notices: use correct American legal equivalents.
5. Market reports: preserve all numbers exactly; translate unit names.
6. Poetry/fiction: preserve meter and tone where possible; mark [verse].
7. Mark genuinely unreadable text as [illegible] — never invent content.
8. If something is uncertain, add [reading uncertain] inline.
9. Preserve page structure: section headings, datelines, paragraph breaks.
10. Period-appropriate English vocabulary where it aids authenticity.

═══════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════
Return ONLY the English translation.
No preamble. No "Here is the translation:". No closing remarks.
Uncertainty notes go INLINE in [brackets], not at the end.
Precede each page's translation with its page marker exactly as shown:
--- Page N of M ---
[translation of page N]"""


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------
def call_claude(pages_to_translate: dict, issue: dict,
                total_pages: int, api_key: str, system_prompt: str,
                max_output_tokens: int, rate_limiter=None) -> dict:
    """
    Send a subset of pages to Claude for translation.

    pages_to_translate: {page_num: corrected_ocr_text}
    Returns: {page_num: translated_text}

    Sends ONLY corrected OCR text. No raw OCR, no HTML, no images.
    """
    ark_id = issue.get('ark_id', '')

    # Build page blocks — corrected OCR text only
    blocks = []
    for pg in sorted(pages_to_translate.keys()):
        ocr = pages_to_translate[pg].strip()
        blocks.append(f'--- Page {pg} of {total_pages} ---\n{ocr}')

    pages_sending = sorted(pages_to_translate.keys())
    prompt = (
        f'ISSUE : {issue.get("full_title", "")}\n'
        f'DATE  : {issue.get("date", "")}\n'
        f'VOL/NO: Volume {issue.get("volume", "")}, Number {issue.get("number", "")}\n'
        f'PAGES : translating pages {pages_sending} of {total_pages}\n'
        f'ARK   : {ark_id}\n\n'
        + '\n\n'.join(blocks)
        + f'\n\nTranslate the above pages to English. '
        f'Begin each page with:\n--- Page N of {total_pages} ---'
    )

    payload = {
        'model':      CLAUDE_MODEL,
        'max_tokens': max_output_tokens,
        'system':     system_prompt,
        'messages':   [{'role': 'user', 'content': prompt}],
    }
    req_data = json.dumps(payload).encode('utf-8')

    # Rate limiter estimate: input ~1,700/page, output ~3,000/page
    n_pages = len(pages_to_translate)
    if rate_limiter:
        rate_limiter.acquire(estimated_tokens=n_pages * (1_700 + 3_000))

    max_retries = 3
    last_error  = None
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(
                ANTHROPIC_API, data=req_data,
                headers={'Content-Type':       'application/json',
                         'x-api-key':          api_key,
                         'anthropic-version': '2023-06-01'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=600) as resp:
                result = json.loads(resp.read())

            if rate_limiter:
                usage = result.get('usage', {})
                rate_limiter.record_usage(
                    input_tokens=usage.get('input_tokens', 0),
                    output_tokens=usage.get('output_tokens', 0),
                )

            stop_reason = result.get('stop_reason', 'end_turn')
            raw_response = ''
            for block in result.get('content', []):
                if block.get('type') == 'text':
                    raw_response = block['text'].strip()
                    break

            if not raw_response:
                # Empty response — return failure markers for all pages
                return {
                    pg: (f'{FAILED_PREFIX}{pg} — empty API response]\n\n'
                         f'[Corrected OCR below]\n{pages_to_translate[pg]}')
                    for pg in pages_to_translate
                }

            return _parse_response(
                raw_response, pages_sending, total_pages,
                pages_to_translate, stop_reason, max_output_tokens
            )

        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 529):
                wait = 30 * attempt
                tprint(f'  [rate limit {e.code}, waiting {wait}s, attempt {attempt}/{max_retries}]')
                time.sleep(wait)
                last_error = e
            else:
                body = e.read().decode('utf-8', errors='replace')
                raise RuntimeError(f'HTTP {e.code}: {body[:200]}') from e
        except Exception as e:
            if attempt < max_retries:
                wait = 15 * attempt
                tprint(f'  [error attempt {attempt}: {e}, retrying in {wait}s]')
                time.sleep(wait)
                last_error = e
            else:
                last_error = e

    raise last_error


def _parse_response(response: str, pages_sent: list, total_pages: int,
                    source_pages: dict, stop_reason: str,
                    max_output_tokens: int) -> dict:
    """
    Parse Claude's response into {page_num: translated_text}.

    Handles truncation (stop_reason == 'max_tokens') gracefully:
    - Pages that were fully translated are kept.
    - The page where truncation happened gets a BUDGET_EXCEEDED marker
      that embeds the corrected OCR so --resume can finish it cheaply.
    - Pages after truncation get the same marker.
    """
    translated    = {}
    current_pg    = None
    current_lines = []

    for line in response.splitlines():
        m = re.match(r'^---\s*Page\s+(\d+)\s+of\s+\d+\s*---\s*$', line.strip())
        if m:
            if current_pg is not None:
                translated[current_pg] = '\n'.join(current_lines).strip()
            current_pg    = int(m.group(1))
            current_lines = []
        elif current_pg is not None:
            current_lines.append(line)

    if current_pg is not None:
        text = '\n'.join(current_lines).strip()
        # If Claude ran out of tokens, the last page it was writing is incomplete.
        # Mark it for retry regardless of whether it was the final page in pages_sent.
        if stop_reason == 'max_tokens':
            translated[current_pg] = (
                f'{BUDGET_EXCEEDED_PREFIX}{current_pg} — '
                f'output was cut at {max_output_tokens} tokens.\n'
                f'Partial translation above (if any), corrected OCR below for retry.]\n\n'
                f'[Corrected OCR for page {current_pg}]\n'
                f'{source_pages.get(current_pg, "[no source]")}'
            )
            if text:
                # Preserve whatever Claude did manage to write before the cutoff
                translated[current_pg] = text + '\n\n' + translated[current_pg]
        else:
            translated[current_pg] = text

    # Any page that should have been in the response but isn't:
    # happens when max_tokens was hit before Claude reached that page.
    for pg in pages_sent:
        if pg not in translated:
            if stop_reason == 'max_tokens':
                translated[pg] = (
                    f'{BUDGET_EXCEEDED_PREFIX}{pg} — '
                    f'output budget of {max_output_tokens} tokens exhausted before this page. '
                    f'Re-run with --resume to complete, or increase --max-output-tokens.]\n\n'
                    f'[Corrected OCR for page {pg}]\n'
                    f'{source_pages.get(pg, "[no source]")}'
                )
            else:
                translated[pg] = (
                    f'{FAILED_PREFIX}{pg} — page missing from response '
                    f'(stop_reason={stop_reason}).]\n\n'
                    f'[Corrected OCR for page {pg}]\n'
                    f'{source_pages.get(pg, "[no source]")}'
                )

    return translated


# ---------------------------------------------------------------------------
# Get best available source text for a page
# ---------------------------------------------------------------------------
def _extract_text_from_ai_ocr(md_text: str) -> str:
    """Extract plain text from an ai_ocr page markdown file.

    Strips the header (above ---), gap tags, and Column/Ad markers,
    returning clean text suitable for translation.
    """
    # Extract body: everything between first --- and STATS:
    # The body may contain its own --- dividers (article separators)
    first_delim = md_text.find("\n---\n")
    if first_delim >= 0:
        body_start = first_delim + 5
        stats_idx = md_text.find("\nSTATS:", body_start)
        if stats_idx > 0:
            text = md_text[body_start:stats_idx]
            if text.rstrip().endswith("---"):
                text = text.rstrip()[:-3]
        else:
            text = md_text[body_start:]
        text = text.strip()
    else:
        text = md_text

    # Strip gap tags → replace with best guess
    text = re.sub(r'\{\{\s*gap\s*\|[^}]*\[([^\]]*)\]\s*\}\}', r'\1', text)
    # Strip Column/Ad markers
    text = re.sub(r'\{\{\s*/?(?:Column|Ad)\d*\s*\}\}', '', text)
    # Strip Img markers
    text = re.sub(r'\{\{\s*Img\s*\|[^}]*\}\}', '', text)
    # Collapse blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def get_source_pages(issue: dict, pages_needed: list) -> tuple[dict, bool]:
    """
    Return (source_pages_dict, using_ai_ocr) for the given page numbers.

    Priority: ai_ocr/ per-page .md files → strip HTML from ocr/ file → empty.
    Source text is always returned as plain text.
    """
    ark_id = issue['ark_id']
    vol    = str(issue.get('volume', '?')).zfill(2)
    num    = str(issue.get('number', '?')).zfill(2)
    date   = re.sub(r'[^\w\-]', '-', issue.get('date', 'unknown'))
    fname  = f'{ark_id}_vol{vol}_no{num}_{date}.txt'

    # Try ai_ocr/ per-page markdown files first
    ai_dir = AI_OCR_DIR / ark_id if AI_OCR_DIR else None
    if ai_dir and ai_dir.exists():
        source = {}
        for pg in pages_needed:
            md_path = ai_dir / f"page_{pg:02d}.md"
            if md_path.exists():
                source[pg] = _extract_text_from_ai_ocr(
                    md_path.read_text(encoding='utf-8', errors='replace'))
            else:
                source[pg] = ''
        if any(v.strip() for v in source.values()):
            return source, True

    # Fall back to raw portal OCR
    ocr_path = OCR_DIR / fname
    if ocr_path.exists():
        _, ocr_pages = parse_pages(ocr_path.read_text(encoding='utf-8', errors='replace'))
        source = {pg: strip_html(ocr_pages.get(pg, '')) for pg in pages_needed}
        return source, False

    return {pg: '' for pg in pages_needed}, False


# ---------------------------------------------------------------------------
# Issue file header from OCR/corrected source
# ---------------------------------------------------------------------------
def get_issue_header(issue: dict) -> str:
    """Return the standard file header from whatever source exists."""
    ark_id = issue['ark_id']
    vol    = str(issue.get('volume', '?')).zfill(2)
    num    = str(issue.get('number', '?')).zfill(2)
    date   = re.sub(r'[^\w\-]', '-', issue.get('date', 'unknown'))
    fname  = f'{ark_id}_vol{vol}_no{num}_{date}.txt'

    for path in [OCR_DIR / fname]:
        if path and path.exists():
            header, _ = parse_pages(path.read_text(encoding='utf-8', errors='replace'))
            if header.strip():
                return header

    # Synthetic header if no source file found
    title = issue.get('full_title', '')
    return (
        f'=== {issue.get("collection_name", "UNT COLLECTION")} ===\n'
        f'ARK:    {ark_id}\n'
        f'Date:   {issue.get("date", "")}\n'
        f'Volume: {issue.get("volume", "")}   Number: {issue.get("number", "")}\n'
        f'Title:  {title}\n'
        f'{"="*60}'
    )


# ---------------------------------------------------------------------------
# Process one issue
# ---------------------------------------------------------------------------
def process_issue(issue: dict, api_key: str, system_prompt: str,
                  resume: bool, max_output_tokens: int,
                  rate_limiter=None, worker_id: str = '',
                  _audit: dict = None) -> str:
    """
    Translate one issue. Caller (run_issue) has already audited the file and
    printed the status line — this function just does the work and prints
    the result (sent N pages / done / error).
    """
    ark_id = issue['ark_id']
    vol    = str(issue.get('volume', '?')).zfill(2)
    num    = str(issue.get('number', '?')).zfill(2)
    date   = re.sub(r'[^\w\-]', '-', issue.get('date', 'unknown'))
    fname  = f'{ark_id}_vol{vol}_no{num}_{date}.txt'
    trans_path = TRANSLATED_DIR / fname

    # Use pre-computed audit from run_issue if provided, else compute now
    audit = _audit if _audit is not None else audit_translated_file(trans_path)

    if resume and audit['status'] == 'complete':
        return 'skipped'

    total_pages = int(issue.get('pages', 8))

    if audit['status'] == 'partial':
        pages_needed = audit['needs_translation']
        existing     = audit['pages']
        header       = audit['header']
        total_pages  = audit['total_pages'] or total_pages
    elif audit['status'] in ('absent', 'empty'):
        pages_needed = list(range(1, total_pages + 1))
        existing     = {}
        header       = get_issue_header(issue)
    else:
        # complete but resume=False — re-translate everything
        pages_needed = list(range(1, total_pages + 1))
        existing     = {}
        header       = audit.get('header') or get_issue_header(issue)

    # ── Get source text (corrected OCR, plain text only) ────────────────
    source_pages, using_corrected = get_source_pages(issue, pages_needed)

    # Warn if any source pages are empty
    missing_source = [pg for pg in pages_needed if not source_pages.get(pg, '').strip()]
    if missing_source:
        tprint(f'  ⚠ No source text for pages {missing_source}', worker=worker_id)

    # Drop pages with no source — write NO_SOURCE marker immediately
    for pg in missing_source:
        existing[pg] = f'{NO_SOURCE_PREFIX}{pg} — no corrected or raw OCR available]'
    pages_to_send = {pg: source_pages[pg] for pg in pages_needed
                     if pg not in missing_source and source_pages.get(pg, '').strip()}

    if not pages_to_send:
        tprint(f'  ✗ no source text available', worker=worker_id)
        write_translated_file(trans_path, header, {**existing}, CLAUDE_MODEL, using_corrected)
        return 'no_source'

    # ── API call ─────────────────────────────────────────────────────────
    try:
        translated = call_claude(
            pages_to_send, issue, total_pages,
            api_key, system_prompt, max_output_tokens,
            rate_limiter=rate_limiter,
        )
    except Exception as e:
        tprint(f'  ✗ API error: {e}', worker=worker_id)
        for pg in pages_to_send:
            existing[pg] = (
                f'{FAILED_PREFIX}{pg} — {e}]\n\n'
                f'[Corrected OCR for page {pg}]\n{pages_to_send[pg]}'
            )
        write_translated_file(trans_path, header, existing, CLAUDE_MODEL, using_corrected)
        return f'error: {e}'

    # ── Merge results ────────────────────────────────────────────────────
    budget_exceeded = []
    for pg, text in translated.items():
        existing[pg] = text
        if text.startswith(BUDGET_EXCEEDED_PREFIX):
            budget_exceeded.append(pg)

    write_translated_file(trans_path, header, existing, CLAUDE_MODEL, using_corrected)

    kb = trans_path.stat().st_size // 1024
    if budget_exceeded:
        tprint(f'  ⚠ Budget exceeded for pages {budget_exceeded} '
               f'— run --resume to complete.  ({kb} KB)', worker=worker_id)
        return 'partial_ok'
    else:
        snippet = translated.get(sorted(pages_to_send.keys())[0], '')[:80].replace('\n', ' ')
        tprint(f'  ✓ "{snippet}..."  ({kb} KB)', worker=worker_id)
        return 'ok'


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description='Translate UNT archive corrected OCR to English using Claude')
    p.add_argument('--config-path',        required=True,
                   help='Path to collection.json')
    p.add_argument('--api-key',            default='',
                   help='Anthropic API key (overrides env / collection.json)')
    p.add_argument('--resume',             action='store_true',
                   help='Skip complete issues; repair partial/broken ones')
    p.add_argument('--ark',                default=None,
                   help='Translate only this ARK ID')
    p.add_argument('--date-from',          default=None)
    p.add_argument('--date-to',            default=None)
    p.add_argument('--max-output-tokens',  type=int, default=DEFAULT_MAX_OUTPUT_TOKENS,
                   help=f'Max output tokens per Claude call '
                        f'(default: {DEFAULT_MAX_OUTPUT_TOKENS}; Sonnet max: 64000). '
                        f'Increase if you see [BUDGET EXCEEDED] markers.')
    p.add_argument('--api-workers',        type=int, default=3,
                   help='Parallel issues (default: 3)')
    p.add_argument('--serial',             action='store_true',
                   help='Process one issue at a time')
    p.add_argument('--tier',               default='default',
                   choices=['default', 'build', 'custom'],
                   help='Anthropic rate limit tier')
    args = p.parse_args()

    config_path = Path(args.config_path)
    if not config_path.exists():
        sys.exit(f'Config not found: {config_path}')
    with open(config_path, encoding='utf-8') as f:
        config = json.load(f)

    collection_dir = config_path.parent
    init_paths(collection_dir)

    # Load global config for API key and model defaults
    global_config_path = Path(__file__).parent / "config.json"
    global_config = {}
    if global_config_path.exists():
        try:
            global_config = json.loads(global_config_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    global CLAUDE_MODEL
    if config.get('claude_model'):
        CLAUDE_MODEL = config['claude_model']
    elif global_config.get('claude_model'):
        CLAUDE_MODEL = global_config['claude_model']

    api_key = (args.api_key
               or os.environ.get('ANTHROPIC_API_KEY', '')
               or global_config.get('anthropic_api_key', '')
               or config.get('anthropic_api_key', ''))
    if not api_key:
        sys.exit('Error: API key required. '
                 'Set in config.json, ANTHROPIC_API_KEY env var, or --api-key flag.')

    index_path = METADATA_DIR / 'all_issues.json'
    if not index_path.exists():
        sys.exit(f'No issue index at {index_path}. Run --discover first.')
    with open(index_path, encoding='utf-8') as f:
        all_issues = json.load(f)

    issues = all_issues
    if args.ark:       issues = [i for i in issues if i['ark_id'] == args.ark]
    if args.date_from: issues = [i for i in issues if i.get('date','') >= args.date_from]
    if args.date_to:   issues = [i for i in issues if i.get('date','') <= args.date_to]

    # ── Audit existing files to count work ──────────────────────────────
    def _trans_path(i):
        ark  = i['ark_id']
        v    = str(i.get('volume','?')).zfill(2)
        n    = str(i.get('number','?')).zfill(2)
        d    = re.sub(r'[^\w\-]', '-', i.get('date','unknown'))
        return TRANSLATED_DIR / f'{ark}_vol{v}_no{n}_{d}.txt'

    corr_count = sum(1 for i in issues
                     if (AI_OCR_DIR / i['ark_id']).exists()
                     and any((AI_OCR_DIR / i['ark_id']).glob("page_*.md")))

    # Print collection summary immediately — before any network calls
    print(f'Collection       : {config["title_name"]}', flush=True)
    print(f'Issues           : {len(issues)}', flush=True)
    print(f'AI OCR           : {corr_count}/{len(issues)}', flush=True)
    print(f'Max output tokens: {args.max_output_tokens}', flush=True)
    print(f'Model            : {CLAUDE_MODEL}', flush=True)
    print(f'Output           : {TRANSLATED_DIR}', flush=True)

    # Audit translated files to count what needs doing — fast (stat calls only)
    complete = partial = absent = 0
    pages_to_process = 0
    for issue in issues:
        tp = _trans_path(issue)
        audit = audit_translated_file(tp)
        if audit['status'] == 'complete':
            complete += 1
            if not args.resume:
                pages_to_process += int(issue.get('pages', 8))
        elif audit['status'] == 'partial':
            partial += 1
            pages_to_process += len(audit.get('needs_translation', []))
        else:
            absent += 1
            pages_to_process += int(issue.get('pages', 8))

    print(f'Work             : {pages_to_process} pages  '
          f'(complete: {complete}, partial/broken: {partial}, absent: {absent})',
          flush=True)
    if partial:
        print(f'  ℹ  {partial} issue(s) have broken/partial translations '
              f'— --resume will repair them without re-translating good pages.',
              flush=True)

    # ── Cost estimate + model selection (makes one /v1/models network call) ─
    if choose_model_and_confirm:
        CLAUDE_MODEL = choose_model_and_confirm(
            api_key=api_key,
            pages_to_process=pages_to_process,
            step_name='Translation',
            input_tok_per_page=1_700,
            output_tok_per_page=3_000,
            default_model=CLAUDE_MODEL,
        )
    else:
        est = (pages_to_process * 1_700 * 3.0 +
               pages_to_process * 3_000 * 15.0) / 1_000_000
        print(f'\n  Estimated cost: ~${est:.2f}  '
              f'({pages_to_process} pages × $3/MTok in + $15/MTok out)')
        if input('Proceed? [y/N]: ').strip().lower() not in ('y', 'yes'):
            sys.exit(0)

    system_prompt = build_system_prompt(config)

    if ClaudeRateLimiter:
        rate_limiter = limiter_from_tier(args.tier)
        print(f'Rate limiter: {args.tier} tier')
    else:
        rate_limiter = None

    workers = 1 if args.serial else args.api_workers
    print(f'Workers: {workers}  '
          f'({"serial" if args.serial else "parallel issues"})\n')

    log           = []
    log_lock      = threading.Lock()
    counters      = {'ok': 0, 'partial_ok': 0, 'skipped': 0, 'err': 0}
    counters_lock = threading.Lock()
    TRANSLATED_DIR.mkdir(parents=True, exist_ok=True)

    def run_issue(idx_issue):
        idx, issue = idx_issue
        ark_id = issue['ark_id']
        wid    = f'w{idx % workers + 1}' if workers > 1 else ''
        vol    = str(issue.get('volume', '?')).zfill(2)
        num    = str(issue.get('number', '?')).zfill(2)
        date   = re.sub(r'[^\w\-]', '-', issue.get('date', 'unknown'))
        fname  = f'{ark_id}_vol{vol}_no{num}_{date}.txt'
        prefix = f'[{idx+1:02d}/{len(issues)}] {ark_id}  Vol.{vol} No.{num}  {issue.get("date","")}'

        # ── Step 1: does a file exist? ───────────────────────────────────
        trans_path = TRANSLATED_DIR / fname
        audit = audit_translated_file(trans_path)

        # ── Step 2: decide what to do and report it immediately ──────────
        if audit['status'] == 'absent' or audit['status'] == 'empty':
            tprint(f'{prefix}  →  no file — translating all {issue.get("pages", 8)} pages',
                   worker=wid)

        elif audit['status'] == 'complete':
            if args.resume:
                # Truly nothing to do — silent, just count
                with counters_lock:
                    counters['skipped'] += 1
                with log_lock:
                    log.append({'ark_id': ark_id, 'date': issue.get('date',''), 'status': 'skipped'})
                return 'skipped'
            else:
                tprint(f'{prefix}  →  re-translating all {issue.get("pages", 8)} pages',
                       worker=wid)

        elif audit['status'] == 'partial':
            needs = audit['needs_translation']
            tprint(f'{prefix}  →  sending {len(needs)} page(s) for correction: {needs}',
                   worker=wid)

        # ── Step 3: do the work ──────────────────────────────────────────
        status = process_issue(
            issue, api_key, system_prompt,
            args.resume, args.max_output_tokens,
            rate_limiter=rate_limiter, worker_id=wid,
            _audit=audit,
        )
        with log_lock:
            log.append({'ark_id': ark_id, 'date': issue.get('date',''), 'status': status})
            log_path = TRANSLATED_DIR / 'translation_log.json'
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(log, f, indent=2)
        with counters_lock:
            if   status == 'ok':          counters['ok']         += 1
            elif status == 'partial_ok':  counters['partial_ok'] += 1
            elif status == 'skipped':     counters['skipped']    += 1
            else:                         counters['err']        += 1
        return status

    if workers == 1:
        for item in enumerate(issues):
            run_issue(item)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(run_issue, item): item for item in enumerate(issues)}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception as e:
                    _, issue = futs[fut]
                    tprint(f'  ✗ Unhandled: {issue["ark_id"]}: {e}')

    if rate_limiter:
        tprint(f'\nRate limiter: {rate_limiter.status_line()}')

    tprint(f'\n{"="*50}')
    tprint(f'Complete   : {counters["ok"]}')
    tprint(f'Partial/OK : {counters["partial_ok"]} (run --resume to finish)')
    tprint(f'Skipped    : {counters["skipped"]} (already complete — not shown above)')
    tprint(f'Errors     : {counters["err"]}')
    tprint(f'Output     : {TRANSLATED_DIR}')

    if counters['partial_ok'] or counters['err']:
        tprint(f'\n  → Re-run with --resume to complete unfinished pages.')


if __name__ == '__main__':
    main()
