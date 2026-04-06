#!/usr/bin/env python3
"""
UNT Archive — AI OCR Correction Pipeline
==========================================
Replaces the multi-engine OCR pipeline with Claude Vision.

Each page image is sent to Claude along with optional ABBYY/portal OCR text
and comprehensive Fraktur reference data. Claude performs direct transcription,
cross-referencing, gap identification, confidence scoring, and article
boundary marking in a single pass.

Output:
  ai_ocr/{ark_id}/page_{NN}.md    Detailed per-page output with all markup
  corrected/{issue}.txt            Clean text for translate pipeline
  articles/{ark_id}/               Segmented articles

USAGE:
  python unt_ocr_correct.py --config-path collection.json --resume
  python unt_ocr_correct.py --config-path collection.json --force
  python unt_ocr_correct.py --config-path collection.json --budget 50.00
"""

import os, sys, json, time, re, base64, argparse, threading
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error

try:
    from claude_rate_limiter import ClaudeRateLimiter, limiter_from_tier
except ImportError:
    ClaudeRateLimiter = None
    def limiter_from_tier(t): return None

try:
    from unt_cost_estimate import load_pricing
except ImportError:
    load_pricing = None

# ============================================================================
# CONSTANTS
# ============================================================================

UNT_BASE      = "https://texashistory.unt.edu"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL  = "claude-sonnet-4-6"
ILLEGIBLE     = "[unleserlich]"  # deprecated — kept only for parsing legacy files

# Token estimates (calibrated: ~56k image + ~7k prompt + ~5k reference OCR)
EST_INPUT_TOKENS_PER_PAGE  = 68_000
EST_OUTPUT_TOKENS_PER_PAGE = 5_000
MAX_OUTPUT_TOKENS          = 16_000

PRELOAD_LOG_NAME = "preload_failures.json"

# ============================================================================
# GLOBALS
# ============================================================================

METADATA_DIR  = None
OCR_DIR       = None
CORRECTED_DIR = None
IMAGES_DIR    = None
ABBYY_DIR     = None
ARTICLES_DIR  = None
AI_OCR_DIR    = None
LOG_LEVEL     = 1


def tprint(*args, worker: str = "", level: int = 1, **kwargs):
    if level > LOG_LEVEL:
        return
    prefix = f"[{worker}] " if worker else ""
    print(f"{prefix}", *args, flush=True, **kwargs)


def init_paths(collection_dir: Path):
    global METADATA_DIR, OCR_DIR, CORRECTED_DIR, IMAGES_DIR
    global ABBYY_DIR, ARTICLES_DIR, AI_OCR_DIR
    METADATA_DIR  = collection_dir / "metadata"
    OCR_DIR       = collection_dir / "ocr"
    CORRECTED_DIR = collection_dir / "corrected"
    IMAGES_DIR    = collection_dir / "images"
    ABBYY_DIR     = collection_dir / "abbyy"
    ARTICLES_DIR  = collection_dir / "articles"
    AI_OCR_DIR    = collection_dir / "ai_ocr"
    for d in [CORRECTED_DIR, ARTICLES_DIR, AI_OCR_DIR]:
        d.mkdir(parents=True, exist_ok=True)


# ============================================================================
# REFERENCE DATA
# ============================================================================

REFERENCES_DIR = Path(__file__).parent / "references"
SKILL_DIR      = Path(__file__).parent / "fraktur-ocr skill"

# Additional skill path from global config (set during init)
_configured_skill_dir = None


def init_skill_dir(global_config: dict):
    """Set skill directory from global config if available."""
    global _configured_skill_dir
    sp = global_config.get("skill_path", "")
    if sp:
        p = Path(sp)
        if p.exists() and (p / "SKILL.md").exists():
            _configured_skill_dir = p


def load_reference(filename: str) -> str:
    """Load reference file. Checks references/, then configured skill dir,
    then default skill dir."""
    search = [REFERENCES_DIR]
    if _configured_skill_dir:
        search.append(_configured_skill_dir)
    search.append(SKILL_DIR)
    for d in search:
        p = d / filename
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


# ============================================================================
# IMAGE HELPERS
# ============================================================================

def local_image_path(ark_id: str, page: int) -> Path:
    return IMAGES_DIR / ark_id / f"page_{page:02d}.jpg"


def image_url_candidates(ark_id: str, page: int) -> list:
    base = f"{UNT_BASE}/iiif/ark:/67531/{ark_id}/m1/{page}"
    return [f"{base}/full/max/0/default.jpg",
            f"{base}/full/1500,/0/default.jpg",
            f"{base}/full/1000,/0/default.jpg",
            f"{UNT_BASE}/ark:/67531/{ark_id}/m1/{page}/thumbnail/"]


def download_image_from_unt(ark_id: str, page: int, max_retries: int = 3):
    hdrs = {"User-Agent": "UNT-Archive-Researcher/1.0", "Accept": "image/jpeg,image/*"}
    for attempt in range(1, max_retries + 1):
        for url in image_url_candidates(ark_id, page):
            try:
                req = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    ct, data = resp.headers.get("Content-Type", ""), resp.read()
                    if len(data) >= 50_000 and ("image" in ct or url.endswith(".jpg")):
                        return data
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(30 * attempt)
            except Exception:
                continue
        if attempt < max_retries:
            time.sleep(10 * attempt)
    return None


def is_valid_cached_image(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= 50_000


def fetch_page_image(ark_id: str, page: int) -> tuple:
    p = local_image_path(ark_id, page)
    if is_valid_cached_image(p):
        return p.read_bytes(), "image/jpeg"
    if p.exists():
        p.unlink()
    data = download_image_from_unt(ark_id, page)
    if data:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return data, "image/jpeg"
    return None, None


# ============================================================================
# OCR FILE PARSING
# ============================================================================

def strip_ocr_html(text: str) -> str:
    if '<' not in text:
        return text
    m = re.search(r'id=["\']ocr-text["\'][^>]*>(.*?)</(?:div|section)',
                  text, re.S | re.I)
    inner = m.group(1) if m else text
    inner = re.sub(r'<br\s*/?>', '\n', inner, flags=re.I)
    inner = re.sub(r'<[^>]{0,500}>', ' ', inner)
    for e, r in [('&amp;', '&'), ('&lt;', ''), ('&gt;', ''),
                 ('&quot;', '"'), ('&nbsp;', ' '), ('&#x27;', "'")]:
        inner = inner.replace(e, r)
    inner = re.sub(r'&#[xX][0-9a-fA-F]{1,6};', '', inner)
    inner = re.sub(r'&#\d{1,6};', '', inner)
    inner = re.sub(r'[ \t]{2,}', ' ', inner)
    inner = re.sub(r'\n{3,}', '\n\n', inner)
    return inner.strip()


def parse_ocr_pages(text: str) -> tuple:
    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
    hlines, blines, in_h = [], [], True
    for line in lines:
        if in_h:
            hlines.append(line)
            if line.startswith('=' * 10):
                in_h = False
        else:
            blines.append(line)
    header = '\n'.join(hlines)
    pages, cur_pg, cur_lines = {}, None, []
    for line in '\n'.join(blines).split('\n'):
        m = re.match(r'^--- Page (\d+) of (\d+) ---$', line)
        if m:
            if cur_pg is not None:
                pages[cur_pg] = '\n'.join(cur_lines).strip()
            cur_pg, cur_lines = int(m.group(1)), []
        elif cur_pg is not None:
            cur_lines.append(line)
    if cur_pg is not None:
        pages[cur_pg] = '\n'.join(cur_lines).strip()
    return header, pages


# ============================================================================
# ABBYY / PORTAL OCR TEXT EXTRACTION
# ============================================================================

def get_abbyy_page_text(issue_fname: str, page_num: int):
    if not ABBYY_DIR or not ABBYY_DIR.exists():
        return None
    xml_fname = issue_fname.replace('.txt', '.xml')
    xml_path = ABBYY_DIR / xml_fname
    if not xml_path.exists():
        return None
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(xml_path)
        root = tree.getroot()
        ns = re.match(r'\{.*\}', root.tag)
        ns_prefix = ns.group(0) if ns else ""
        pages = list(root.iter(f'{ns_prefix}page'))
        if page_num - 1 >= len(pages):
            return None
        page = pages[page_num - 1]
        texts = []
        for block in page.iter(f'{ns_prefix}block'):
            if block.get('blockType', '') != 'Text':
                continue
            for line in block.iter(f'{ns_prefix}line'):
                line_text = []
                for fmt in line.iter(f'{ns_prefix}formatting'):
                    chars = []
                    for char_el in fmt.iter(f'{ns_prefix}charParams'):
                        chars.append(char_el.text or '')
                    line_text.append(''.join(chars))
                texts.append(' '.join(line_text))
        return '\n'.join(texts) if texts else None
    except Exception:
        return None


def get_portal_ocr_text(issue_fname: str, page_num: int):
    """Get portal OCR text for a specific page, stripping HTML."""
    ocr_path = OCR_DIR / issue_fname
    if not ocr_path.exists():
        return None
    raw = ocr_path.read_text(encoding="utf-8", errors="replace")

    # Try structured format first (has --- Page N of M --- markers)
    _, pages = parse_ocr_pages(raw)
    if pages:
        page_text = pages.get(page_num, "")
        if page_text:
            return strip_ocr_html(page_text)
        return None

    # Fallback: raw HTML without page markers (single-page OCR dump).
    # Strip HTML and return the whole thing for page 1, or None for others.
    stripped = strip_ocr_html(raw)
    if stripped and page_num == 1:
        return stripped
    # For multi-page raw HTML with no markers, return the full text
    # for any page — better than nothing, Claude can cross-reference.
    if stripped:
        return stripped
    return None


# ============================================================================
# COST TRACKING
# ============================================================================

class CostTracker:
    """Track actual API costs during a batch run."""

    def __init__(self, model: str, budget=None):
        self._lock = threading.Lock()
        self.model = model
        self.budget = budget  # None or float
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0
        self.pages_processed = 0
        self.input_price = 0.0   # $/MTok
        self.output_price = 0.0  # $/MTok

        if load_pricing:
            pricing = load_pricing()
            mp = pricing.get(model, {})
            self.input_price = mp.get("input", 0.0)
            self.output_price = mp.get("output", 0.0)

    def record(self, input_tokens: int, output_tokens: int):
        with self._lock:
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens
            cost = (input_tokens * self.input_price +
                    output_tokens * self.output_price) / 1_000_000
            self.total_cost += cost
            self.pages_processed += 1

    def estimate_remaining(self, pages_left: int) -> float:
        with self._lock:
            if self.pages_processed == 0:
                return pages_left * (
                    EST_INPUT_TOKENS_PER_PAGE * self.input_price +
                    EST_OUTPUT_TOKENS_PER_PAGE * self.output_price
                ) / 1_000_000
            avg_in = self.total_input_tokens / self.pages_processed
            avg_out = self.total_output_tokens / self.pages_processed
            return pages_left * (
                avg_in * self.input_price + avg_out * self.output_price
            ) / 1_000_000

    def would_exceed_budget(self) -> bool:
        if self.budget is None:
            return False
        with self._lock:
            if self.pages_processed == 0:
                avg_cost = (EST_INPUT_TOKENS_PER_PAGE * self.input_price +
                            EST_OUTPUT_TOKENS_PER_PAGE * self.output_price) / 1_000_000
            else:
                avg_cost = self.total_cost / self.pages_processed
            return (self.total_cost + avg_cost) > self.budget

    def avg_cost_per_page(self) -> float:
        with self._lock:
            if self.pages_processed == 0:
                return (EST_INPUT_TOKENS_PER_PAGE * self.input_price +
                        EST_OUTPUT_TOKENS_PER_PAGE * self.output_price) / 1_000_000
            return self.total_cost / self.pages_processed

    def summary(self) -> str:
        with self._lock:
            s = (f"Pages: {self.pages_processed}  "
                 f"Tokens: {self.total_input_tokens:,}in + "
                 f"{self.total_output_tokens:,}out  "
                 f"Cost: ${self.total_cost:.2f}")
            if self.budget is not None:
                s += (f"  Budget: ${self.budget:.2f}  "
                      f"Remaining: ${self.budget - self.total_cost:.2f}")
            return s


# ============================================================================
# CLAUDE API
# ============================================================================

def claude_api_call(payload: dict, api_key: str,
                    rate_limiter=None, est_tokens: int = 8000,
                    cost_tracker=None):
    """Make a Claude API call with retry/rate-limit. Returns (text, usage)."""
    tprint(f"    -> Claude API: model={payload.get('model')} "
           f"max_tokens={payload.get('max_tokens')} est={est_tokens}", level=3)
    req_data = json.dumps(payload).encode("utf-8")
    if rate_limiter:
        rate_limiter.acquire(estimated_tokens=est_tokens)

    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                ANTHROPIC_API, data=req_data,
                headers={"Content-Type": "application/json",
                         "x-api-key": api_key,
                         "anthropic-version": "2023-06-01"},
                method="POST")
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read())
            usage = result.get("usage", {})
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            if rate_limiter:
                rate_limiter.record_usage(input_tokens=in_tok, output_tokens=out_tok)
            if cost_tracker:
                cost_tracker.record(in_tok, out_tok)
            tprint(f"    <- Claude API: {in_tok} in + {out_tok} out tokens",
                   level=3)
            text = ""
            for block in result.get("content", []):
                if block.get("type") == "text":
                    text = block["text"].strip()
                    break
            return text, usage
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 529):
                wait = 30 * attempt
                tprint(f" [rate limit {e.code}, wait {wait}s]", level=1)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            if attempt < 3:
                time.sleep(15 * attempt)
            else:
                raise
    raise RuntimeError("Max retries exceeded")


# ============================================================================
# SYSTEM PROMPT BUILDING
# ============================================================================

def build_system_prompt(config: dict) -> str:
    """Build comprehensive system prompt with reference data from the
    fraktur-ocr skill and collection metadata."""
    title      = config.get("title_name", "")
    publisher  = config.get("publisher", "")
    location   = config.get("pub_location", "Texas")
    date_range = config.get("date_range", "")
    language   = config.get("language", "German")
    typeface   = config.get("typeface", "Fraktur")
    source     = config.get("source_medium", "microfilm")
    community  = config.get("community_desc", "")
    places     = config.get("place_names", "")
    orgs       = config.get("organizations", "")
    history    = config.get("historical_context", "")
    subjects   = config.get("subject_notes", "")
    lccn       = config.get("lccn", "")

    fraktur_errors = load_reference("fraktur-errors.md")
    texas_german   = load_reference("texas-german.md")
    markup_spec    = load_reference("markup-spec.md")

    ctx = ""
    if community: ctx += f"\nCOMMUNITY: {community}"
    if history:   ctx += f"\nHISTORY: {history}"
    if subjects:  ctx += f"\nSUBJECTS: {subjects}"
    if places:    ctx += f"\nPLACE NAMES (preserve exactly): {places}"
    if orgs:      ctx += f"\nORGANIZATIONS (preserve exactly): {orgs}"

    return f"""You are an expert OCR specialist running the fraktur-ocr pipeline
for 19th-century German Fraktur newspaper text scanned from microfilm.

COLLECTION: {title}
Publisher: {publisher} | Location: {location} | Period: {date_range}
Language: {language} | Typeface: {typeface} | Source: {source}
{"LCCN: " + lccn if lccn else ""}
{ctx}

REFERENCE: FRAKTUR ERROR PATTERNS
===================================
{fraktur_errors}

REFERENCE: TEXAS GERMAN VOCABULARY & ORTHOGRAPHY
==================================================
{texas_german}

REFERENCE: METADATA MARKUP SPECIFICATION
==========================================
{markup_spec}

THE THREE-PASS PIPELINE
========================

You receive a newspaper page image and optionally ABBYY/portal OCR text.
Execute all three passes and return a single combined result.

PASS 1 - DIRECT FRAKTUR OCR:
1. Identify the page layout: masthead, column count, center-page features
   (advertisements, program announcements, large headlines), damage
2. Read each section in order: masthead, center features, columns
   left-to-right top-to-bottom
3. Transcribe the Fraktur text to Latin characters
4. Apply Fraktur error corrections (Tiers 1-5) automatically as you read:
   - Tier 1 aggressively (b/d, f/s swaps are almost always correct)
   - Tier 2 with context checking (capital letter confusions)
   - Tiers 3-5 case by case (ligature breaks, number/letter, hyphenation)
5. Where text is confidently readable, write it directly with no tags
6. Where text is illegible or uncertain, insert a gap tag with:
   - est = estimated character count
   - imgbbox = approximate pixel bounding box (x,y=top-left, w,h=size), be generous
   - cnf = confidence 0-1 (0.95=high, 0.5=moderate, 0.0=pure guess)
   - [best guess] = your prediction in square brackets
   {{{{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX" [best guess] }}}}
   ALWAYS guess. Set cnf="0.00" if it's pure context inference.
7. After cross-referencing with ABBYY/portal OCR, add region_ocr (raw text):
   {{{{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX" | fragments="partial" | region_ocr="raw" [guess] }}}}
8. Mark images/illustrations/engravings:
   {{{{ Img | bbox="x,y,w,h" | desc="brief description" }}}}
9. Wrap each article/news item/notice in a numbered Column tag:
   {{{{ Column001 }}}} article text {{{{ /Column }}}}
10. Wrap each advertisement in a numbered Ad tag:
   {{{{ Ad001 }}}} ad text {{{{ /Ad }}}}
11. Number Column and Ad tags sequentially per page (001, 002, 003...)
12. Do NOT correct Texas German dialect words or pre-1901 spellings
13. Do NOT translate English loanwords to German
14. Headlines: ## text | Subheads: ### text | Datelines: **City, Date**

PASS 2 - GAP INVENTORY:
For each {{{{ gap }}}} from Pass 1:
1. Re-examine the image at that location
2. Note partial letterforms, ascenders, descenders, dots, fragments
3. Refine the character count estimate, bounding box, and your best guess
4. Add fragments field and adjust cnf:
   {{{{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX" | fragments="visible" [refined guess] }}}}

PASS 3 - CROSS-REFERENCE AND CONFIDENCE:
For each gap:
1. If ABBYY/portal OCR is provided, find the corresponding region
2. Apply the Fraktur error correction table to decode the raw OCR fragment
3. Cross-reference: your reading + OCR fragment + 1890s German +
   article topic (international news = more Hochdeutsch, local = more dialect)
4. Update the gap tag: refine your guess, adjust cnf, add region_ocr:
   {{{{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX" | fragments="..." | region_ocr="raw_ocr" [best guess] }}}}
   region_ocr MUST contain the exact raw OCR text, uncorrected.
5. For non-gap corrections where the fix is ambiguous:
   corrected_word <!-- {{{{ corrected | original="ocr_reading" | rule="rule_name" }}}} -->

cnf scale: 0.90-0.99=high, 0.70-0.89=moderate, 0.40-0.69=low,
0.01-0.39=speculative, 0.00=pure context guess. Never 1.00.

OUTPUT FORMAT - return this exact structure:

LAYOUT: <one-line layout description>
COLUMNS: <number>
DAMAGE: <brief damage notes or "none">

---

<final corrected text with all markup tags>

---

STATS:
- estimated_chars: <N>
- chars_no_gap: <N>
- chars_cnf_high: <N>
- chars_cnf_mid: <N>
- chars_cnf_low: <N>
- total_gaps: <N>

CRITICAL RULES:
- NEVER use [unleserlich] or leave text blank
- Every gap MUST have est, imgbbox, cnf, and [best guess]
- Preserve pre-1901 German spellings (thun, Noth, Theil, Eigenthum, -iren)
- Preserve English loanwords as-is (Saloon, County, Farmer, Sheriff, Receiver)
- Preserve Texas German dialect and hybrid forms (Dry Goods Haus, Stadtmarshall)
- If a word is only 1-2 characters off from plausible Texas German, prefer dialect
- Do NOT translate - text stays in original language
- Do NOT modernize period orthography
- Watch for column interleaving (sudden topic change mid-sentence) and mark with
  <!-- {{{{ column_break | from=N | to=M }}}} -->"""


def build_page_prompt(ark_id: str, page_num: int, total_pages: int,
                      newspaper: str, date: str,
                      abbyy_text=None, portal_ocr=None) -> list:
    """Build user message content for a page OCR call."""
    content = []

    img_bytes, img_type = fetch_page_image(ark_id, page_num)
    if img_bytes:
        content.append({"type": "image", "source": {
            "type":       "base64",
            "media_type": img_type or "image/jpeg",
            "data":       base64.standard_b64encode(img_bytes).decode("ascii"),
        }})

    text_parts = [
        f"NEWSPAPER: {newspaper}",
        f"DATE: {date}",
        f"PAGE: {page_num} of {total_pages}",
        f"ARK: {ark_id}",
        "",
    ]

    if not img_bytes:
        text_parts.append(
            "WARNING: Page image unavailable. Work from reference OCR only.")
        text_parts.append("")

    # Reference OCR cap: a dense newspaper page can be 15-25k chars.
    # 30k chars ≈ 7500 tokens — worth sending for cross-referencing.
    OCR_TEXT_CAP = 30_000

    if abbyy_text:
        text_parts.append(
            "ABBYY OCR TEXT (raw, may contain errors - use for cross-referencing):")
        text_parts.append("```")
        if len(abbyy_text) > OCR_TEXT_CAP:
            text_parts.append(
                abbyy_text[:OCR_TEXT_CAP] + "\n[... truncated ...]")
        else:
            text_parts.append(abbyy_text)
        text_parts.append("```")
        text_parts.append("")
    elif portal_ocr:
        text_parts.append(
            "PORTAL OCR TEXT (stripped from UNT portal HTML, may contain "
            "errors - use for cross-referencing):")
        text_parts.append("```")
        if len(portal_ocr) > OCR_TEXT_CAP:
            text_parts.append(
                portal_ocr[:OCR_TEXT_CAP] + "\n[... truncated ...]")
        else:
            text_parts.append(portal_ocr)
        text_parts.append("```")
        text_parts.append("")
    else:
        text_parts.append("No reference OCR text available. Work from image only.")
        text_parts.append("")

    text_parts.append(
        "Please transcribe and correct this page now. "
        "Follow the output format exactly.")

    content.append({"type": "text", "text": "\n".join(text_parts)})
    return content


# ============================================================================
# PAGE CORRECTION — MAIN AI CALL
# ============================================================================

def correct_page(ark_id, page_num, total_pages, newspaper, date, issue_fname,
                 system_prompt, api_key, rate_limiter=None, cost_tracker=None):
    """
    Correct a single page using Claude Vision.
    Returns dict: text, markdown, stats, status, usage.
    """
    abbyy_text = get_abbyy_page_text(issue_fname, page_num)
    portal_ocr = get_portal_ocr_text(issue_fname, page_num)

    content = build_page_prompt(
        ark_id, page_num, total_pages, newspaper, date,
        abbyy_text=abbyy_text, portal_ocr=portal_ocr)

    has_image = any(c.get("type") == "image" for c in content)
    if not has_image and not abbyy_text and not portal_ocr:
        return {"text": "", "markdown": "", "stats": {}, "status": "no_image"}

    est = EST_INPUT_TOKENS_PER_PAGE if has_image else 5_000

    try:
        raw, usage = claude_api_call(
            {"model": CLAUDE_MODEL, "max_tokens": MAX_OUTPUT_TOKENS,
             "system": system_prompt,
             "messages": [{"role": "user", "content": content}]},
            api_key, rate_limiter, est_tokens=est, cost_tracker=cost_tracker)
    except Exception as e:
        tprint(f"    x p{page_num:02d} API error: {e}", level=1)
        return {"text": "", "markdown": "", "stats": {},
                "status": "failed", "error": str(e)}

    if not raw.strip():
        return {"text": "", "markdown": "", "stats": {},
                "status": "failed", "error": "empty response"}

    clean_text = extract_clean_text(raw)
    stats = extract_stats(raw)

    ref_desc = ("ABBYY XML" if abbyy_text else
                ("Portal OCR" if portal_ocr else "none"))
    markdown = build_page_markdown(
        newspaper, date, page_num, source_image=f"page_{page_num:02d}.jpg",
        ref_desc=ref_desc, raw_response=raw, stats=stats)

    return {
        "text": clean_text,
        "markdown": markdown,
        "raw_response": raw,
        "stats": stats,
        "status": "ok",
        "usage": usage,
    }


def extract_clean_text(raw_response: str) -> str:
    """Extract clean corrected text from Claude's response, stripping markup."""
    # Find text between --- delimiters
    parts = raw_response.split("\n---\n")
    if len(parts) >= 3:
        text_body = parts[1]
    elif len(parts) == 2:
        text_body = parts[1]
    else:
        text_body = raw_response

    # Strip header lines (LAYOUT/COLUMNS/DAMAGE)
    lines = text_body.strip().split('\n')
    clean_lines = []
    skip_header = True
    for line in lines:
        if skip_header:
            if line.startswith(('LAYOUT:', 'COLUMNS:', 'DAMAGE:')):
                continue
            if line.strip() == '' and not clean_lines:
                continue
            skip_header = False
        if line.startswith('STATS:'):
            break
        clean_lines.append(line)

    text = '\n'.join(clean_lines).strip()

    # Strip HTML comment metadata tags
    text = re.sub(
        r'<!--\s*\{\{\s*(?:corrected|column_break|interleaved)'
        r'[^}]*\}\}\s*-->',
        '', text)

    # Strip structural tags: {{ Column001 }}, {{ /Column }}, {{ Ad001 }}, {{ /Ad }}
    text = re.sub(r'\{\{\s*(?:Column|Ad)\d{3}\s*\}\}', '', text)
    text = re.sub(r'\{\{\s*/(?:Column|Ad)\s*\}\}', '', text)

    # Strip image markers: {{ Img | bbox="..." | desc="..." }}
    text = re.sub(r'\{\{\s*Img\s*\|[^}]*\}\}', '', text)

    # Extract best guess from gap markers (may contain imgbbox and other fields):
    # {{ gap | est=NN | imgbbox="..." | ... [guess] }} -> guess
    text = re.sub(
        r'\{\{\s*gap\s*\|[^[]*\[([^\]]*)\]\s*\}\}',
        r'\1', text)
    # Fallback for old-style gaps without guess
    text = re.sub(r'\{\{\s*gap\s*\|[^}]*\}\}', '', text)
    text = re.sub(r'\{\{\s*gap\s*\}\}', '', text)

    # Strip markdown heading markers for plain text
    text = re.sub(r'^##\s+', '', text, flags=re.M)
    text = re.sub(r'^###\s+', '', text, flags=re.M)

    # Strip bold markers
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)

    # Clean up excess whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def extract_stats(raw_response: str) -> dict:
    stats = {}
    in_stats = False
    for line in raw_response.split('\n'):
        if line.strip().startswith('STATS:'):
            in_stats = True
            continue
        if in_stats:
            m = re.match(r'-\s*(\w+):\s*(\d+)', line.strip())
            if m:
                stats[m.group(1)] = int(m.group(2))
            elif line.strip() and not line.strip().startswith('-'):
                break
    return stats


def build_page_markdown(newspaper, date, page_num, source_image,
                        ref_desc, raw_response, stats):
    """Build the full per-page markdown output."""
    today = datetime.now().strftime("%Y-%m-%d")
    header = f"# {newspaper} -- {date} -- Page {page_num}\n"
    header += "## OCR Pipeline Output\n\n"
    header += "### Processing Metadata\n"
    header += f"- Source image: {source_image}\n"
    header += f"- Reference OCR: {ref_desc}\n"
    header += f"- Processing date: {today}\n"
    header += f"- Model: {CLAUDE_MODEL}\n"
    header += "- Pipeline version: 2.0 (AI-only)\n\n"
    header += "### Statistics\n"
    for key, val in stats.items():
        header += f"- {key}: {val}\n"
    header += "\n---\n\n"

    # Extract text body from raw response (between --- markers)
    parts = raw_response.split("\n---\n")
    if len(parts) >= 2:
        text_body = parts[1]
    else:
        text_body = raw_response

    return header + text_body.strip() + "\n"


# ============================================================================
# ARTICLE SEGMENTATION
# ============================================================================

SEGMENTATION_PROMPT = """You are an expert in historical German newspaper structure.

You receive the corrected OCR text of ONE PAGE from an 1891 German-language
Texas newspaper.

TASK: Segment the page into discrete articles and advertisements.

RULES:
  - Each article, ad, notice, poem, or classified is a SEPARATE item
  - Headlines and datelines (e.g. "Berlin, 3. Sept.") mark article starts
  - Masthead (newspaper title/date/volume) = type "masthead"
  - Legal notices (Bekanntmachung, Aufruf) = type "notice"
  - Poetry or verse = type "poetry"
  - Wire-service items with datelines = type "article"
  - Commercial content = type "advertisement"
  - Default for news = type "article"
  - If an item clearly starts mid-sentence, set continues_from_prev: true
  - If an item clearly ends mid-sentence, set continues_to_next: true
  - Preserve {{ gap }} markers exactly as-is
  - Preserve {{ Column }}, {{ Ad }}, {{ /Column }}, {{ /Ad }} markers

OUTPUT - valid JSON only, no markdown:
{"page": <int>, "items": [{"type": "article|advertisement|masthead|notice|poetry",
  "headline": "<first line or empty>", "body": "<full text>",
  "continues_from_prev": false, "continues_to_next": false}]}"""


def segment_page(page_num, corrected_text, api_key,
                 rate_limiter=None, cost_tracker=None):
    """Segment corrected page text into discrete articles/ads."""
    if not corrected_text.strip():
        return []

    # Simple heuristic: count potential article boundaries
    lines = corrected_text.split('\n')
    content_lines = [l for l in lines if l.strip()]
    headline_count = 0
    for i, line in enumerate(content_lines):
        stripped = line.strip()
        if i > 0 and len(stripped) < 60 and stripped:
            prev = content_lines[i - 1].strip() if i > 0 else ""
            if not prev:
                headline_count += 1

    # Simple page: return as single article
    if headline_count <= 1:
        return [{"type": "article", "headline": "", "body": corrected_text,
                 "page": page_num, "page_span": [page_num, page_num],
                 "continues_from_prev": False, "continues_to_next": False}]

    # Complex page: use Claude
    raw, _ = claude_api_call(
        {"model": CLAUDE_MODEL, "max_tokens": 4000,
         "system": SEGMENTATION_PROMPT,
         "messages": [{"role": "user", "content":
             f"PAGE {page_num}\n\n{corrected_text}"}]},
        api_key, rate_limiter, est_tokens=3000, cost_tracker=cost_tracker)
    try:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
        data = json.loads(clean)
        items = data.get("items", [])
        for item in items:
            item["page"] = page_num
            item["page_span"] = [page_num, page_num]
        return items
    except Exception as e:
        tprint(f"    ! Segmentation parse error p{page_num}: {e}", level=2)
        return [{"type": "article", "headline": "", "body": corrected_text,
                 "page": page_num, "page_span": [page_num, page_num],
                 "continues_from_prev": False, "continues_to_next": False}]


# ============================================================================
# PAGE-BOUNDARY STITCHING
# ============================================================================

STITCH_PROMPT = """You are an expert in historical German newspaper layout.

You receive the LAST ITEM from page N and the FIRST ITEM from page N+1.
Determine if they are the same article continued across the page break.

MERGE signals: last item ends mid-sentence, first item has no headline,
same topic/voice, either flagged continues_to/from.

SEPARATE signals: first item has a headline or dateline, clear topic change,
last item ends with period or closing.

OUTPUT - valid JSON only: {"decision": "merge"|"separate", "reason": "<one sentence>"}"""


def stitch_boundary(last_item, first_item, api_key,
                    rate_limiter=None, cost_tracker=None):
    last_body  = last_item.get("body", "").strip()
    first_body = first_item.get("body", "").strip()
    first_hl   = first_item.get("headline", "").strip()

    if first_hl and len(first_hl) > 3:
        return "separate"
    if (last_body and last_body[-1] in '.!?"'
            and not last_item.get("continues_to_next")):
        return "separate"
    if (last_item.get("continues_to_next")
            and first_item.get("continues_from_prev")):
        return "merge"

    prompt = (
        f"PAGE {last_item.get('page')} last item "
        f"(type={last_item.get('type')}):\n"
        f"...{last_body[-600:]}\n\n"
        f"PAGE {first_item.get('page')} first item "
        f"(type={first_item.get('type')}, headline={first_hl!r}):\n"
        f"{first_body[:600]}...")
    raw, _ = claude_api_call(
        {"model": CLAUDE_MODEL, "max_tokens": 100,
         "system": STITCH_PROMPT,
         "messages": [{"role": "user", "content": prompt}]},
        api_key, rate_limiter, est_tokens=500, cost_tracker=cost_tracker)
    try:
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        dec = json.loads(clean).get("decision", "separate")
        return dec if dec in ("merge", "separate") else "separate"
    except Exception:
        return "separate"


def stitch_all_pages(all_items, api_key, rate_limiter=None,
                     cost_tracker=None, worker_id=""):
    if not all_items:
        return all_items
    pages = {}
    for item in all_items:
        pages.setdefault(item["page"], []).append(item)
    sorted_pgs = sorted(pages.keys())
    merges = 0
    for i in range(len(sorted_pgs) - 1):
        pa, pb = sorted_pgs[i], sorted_pgs[i + 1]
        if not pages[pa] or not pages[pb]:
            continue
        last_item = pages[pa][-1]
        first_item = pages[pb][0]
        if last_item.get("type") in ("masthead", "advertisement"):
            continue
        tprint(f"    stitch p{pa}->p{pb} ...", worker=worker_id, level=3)
        decision = stitch_boundary(
            last_item, first_item, api_key, rate_limiter, cost_tracker)
        if decision == "merge":
            merged_body = (last_item["body"].rstrip() + "\n\n"
                           + first_item["body"].lstrip())
            last_item["body"] = merged_body
            last_item["page_span"] = [
                last_item["page_span"][0],
                max(last_item["page_span"][-1],
                    first_item["page_span"][-1])]
            last_item["continues_to_next"] = first_item.get(
                "continues_to_next", False)
            pages[pb].pop(0)
            merges += 1
            tprint(f"    merged p{pa}->p{pb}", worker=worker_id, level=3)
    if merges:
        tprint(f"  Stitched {merges} cross-page article(s)",
               worker=worker_id, level=1)
    result = []
    for pg in sorted_pgs:
        result.extend(pages.get(pg, []))
    return result


# ============================================================================
# ARTICLE FILE WRITING
# ============================================================================

def write_article_files(issue, all_items, ark_dir):
    """Write one .txt file per article/ad. Also writes manifest.json."""
    ark_dir.mkdir(parents=True, exist_ok=True)
    for old in ark_dir.glob("*_art*.txt"):
        if old.name != "manifest.json":
            old.unlink()

    ark_id     = issue["ark_id"]
    full_title = issue.get("full_title", "")
    total_pgs  = issue.get("pages", 8)
    issue_date = re.sub(r"[^\w\-]", "-", issue.get("date", "unknown"))
    manifest   = []
    art_num    = 1

    for item in all_items:
        pg_first = item["page_span"][0]
        pg_last  = item["page_span"][-1]
        spans = f"{pg_first}-{pg_last}" if pg_first != pg_last else str(pg_first)
        fname = f"{ark_id}_{issue_date}_art{art_num:03d}.txt"

        lines = [
            f"ARK:     {ark_id}",
            f"ISSUE:   {full_title}",
            f"PAGE:    {pg_first}  (of {total_pgs})",
        ]
        if pg_first != pg_last:
            lines.append(f"SPANS:   {spans}")
        lines.append(f"TYPE:    {item.get('type', 'article')}")
        lines.append("")

        headline = item.get("headline", "").strip()
        if headline:
            lines.append(headline)
            lines.append("")
        lines.append(item.get("body", "").strip())

        (ark_dir / fname).write_text("\n".join(lines), encoding="utf-8")
        manifest.append({
            "file":     fname,
            "page":     pg_first,
            "spans":    spans,
            "type":     item.get("type", "article"),
            "headline": headline[:80] if headline else "",
        })
        art_num += 1

    (ark_dir / "manifest.json").write_text(
        json.dumps({"ark_id": ark_id, "issue": full_title,
                    "articles": manifest}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    return len(all_items)


# ============================================================================
# IMAGE PRELOADING
# ============================================================================

def load_preload_log():
    p = IMAGES_DIR / PRELOAD_LOG_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return {}


def save_preload_log(failures):
    (IMAGES_DIR / PRELOAD_LOG_NAME).write_text(
        json.dumps(failures, indent=2), encoding="utf-8")


def _dl_one(task):
    if task["skip"]:
        return {**task, "status": task.get("skip_status", "skipped")}
    ip = task["img_path"]
    if ip.exists() and not is_valid_cached_image(ip):
        try:
            ip.unlink()
        except Exception:
            pass
    data = download_image_from_unt(task["ark_id"], task["page"], max_retries=3)
    if data:
        try:
            ip.parent.mkdir(parents=True, exist_ok=True)
            ip.write_bytes(data)
        except Exception as e:
            return {**task, "status": "failed", "error": str(e)}
        return {**task, "status": "ok", "kb": len(data) // 1024}
    return {**task, "status": "failed", "error": "all URLs returned stubs"}


def preload_images(issues, resume=True, retry_failed=False, workers=4):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    failures, fl = load_preload_log(), threading.Lock()
    ctr = {"downloaded": 0, "skipped": 0, "failed": 0}
    cl = threading.Lock()
    for issue in issues:
        (IMAGES_DIR / issue["ark_id"]).mkdir(exist_ok=True)
    total = sum(int(i.get("pages", 8)) for i in issues)
    valid = sum(1 for i in issues
                for pg in range(1, int(i.get("pages", 8)) + 1)
                if is_valid_cached_image(local_image_path(i["ark_id"], pg)))
    print(f"Preload: {total} pages, {valid} cached", flush=True)
    tasks = []
    for issue in issues:
        for pg in range(1, int(issue.get("pages", 8)) + 1):
            path = local_image_path(issue["ark_id"], pg)
            fk = f"{issue['ark_id']}/page_{pg:02d}"
            skip = (is_valid_cached_image(path)
                    or (not retry_failed and fk in failures))
            tasks.append({
                "ark_id": issue["ark_id"], "page": pg, "img_path": path,
                "fail_key": fk, "skip": skip,
                "skip_status": ("skipped" if is_valid_cached_image(path)
                                else "skip_fail"),
                "vol": issue.get("volume", "?"),
                "num": issue.get("number", "?"),
            })
    announced = set()

    def handle(r):
        if r["ark_id"] not in announced:
            announced.add(r["ark_id"])
            print(f"  {r['ark_id']}  Vol.{r['vol']} No.{r['num']}",
                  flush=True)
        s = r["status"]
        if s == "ok":
            print(f"    p{r['page']:02d} ok  {r.get('kb', 0)}KB", flush=True)
            with cl:
                ctr["downloaded"] += 1
            with fl:
                if r["fail_key"] in failures:
                    del failures[r["fail_key"]]
                    save_preload_log(failures)
        elif s == "failed":
            print(f"    p{r['page']:02d} FAILED  {r.get('error', '')}",
                  flush=True)
            with cl:
                ctr["failed"] += 1
            with fl:
                failures[r["fail_key"]] = {
                    "ark_id": r["ark_id"], "page": r["page"],
                    "attempts": failures.get(
                        r["fail_key"], {}).get("attempts", 0) + 1}
                save_preload_log(failures)
        else:
            with cl:
                ctr["skipped"] += 1

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_dl_one, t): t for t in tasks}
        for fut in as_completed(futs):
            try:
                handle(fut.result())
            except Exception as e:
                handle({**futs[fut], "status": "failed", "error": str(e)})
    print(f"\nPreload: {ctr['downloaded']} downloaded, "
          f"{ctr['skipped']} skipped, {ctr['failed']} failed", flush=True)


# ============================================================================
# ISSUE PROCESSING
# ============================================================================

def process_issue(issue, api_key, system_prompt, delay,
                  resume, force, rate_limiter=None,
                  cost_tracker=None, worker_id=""):
    """Process one newspaper issue through the AI OCR pipeline."""
    ark_id = issue["ark_id"]
    vol    = str(issue.get("volume", "?")).zfill(2)
    num    = str(issue.get("number", "?")).zfill(2)
    date   = re.sub(r"[^\w\-]", "-", issue.get("date", "unknown"))
    fname  = f"{ark_id}_vol{vol}_no{num}_{date}.txt"
    newspaper = issue.get("full_title", issue.get("title", ""))

    ocr_path   = OCR_DIR / fname
    corr_path  = CORRECTED_DIR / fname
    ark_dir    = ARTICLES_DIR / ark_id
    ai_ocr_dir = AI_OCR_DIR / ark_id

    # Resume check
    if resume and not force and corr_path.exists() and corr_path.stat().st_size > 500:
        if ark_dir.exists() and any(ark_dir.glob("*_art*.txt")):
            tprint(f"  SKIP {fname} (already complete)",
                   worker=worker_id, level=1)
            return "skipped"

    # Get page count
    actual_pages = int(issue.get("pages", 8))
    header = ""
    if ocr_path.exists():
        ocr_raw = ocr_path.read_text(encoding="utf-8", errors="replace")
        header, ocr_pages = parse_ocr_pages(ocr_raw)
        if ocr_pages:
            actual_pages = max(ocr_pages.keys())

    # Build header if we don't have one
    if not header or not header.strip():
        title_line = issue.get("full_title", newspaper)
        header = (
            f"=== {title_line} ===\n"
            f"ARK:    {ark_id}\n"
            f"URL:    https://texashistory.unt.edu/ark:/67531/{ark_id}/\n"
            f"Date:   {issue.get('date', 'unknown')}\n"
            f"Volume: {issue.get('volume', '?')}   "
            f"Number: {issue.get('number', '?')}\n"
            f"Title:  {title_line}\n"
            f"{'=' * 60}")

    tprint(f"  {actual_pages}pp  AI OCR correction", worker=worker_id, level=1)

    # Existing corrected pages for partial resume
    existing_corrected = {}
    if resume and not force and corr_path.exists():
        _, existing_corrected = parse_ocr_pages(
            corr_path.read_text(encoding="utf-8", errors="replace"))

    ai_ocr_dir.mkdir(parents=True, exist_ok=True)
    corrected_pages = dict(existing_corrected)

    for pg in range(1, actual_pages + 1):
        # Skip already-corrected pages
        page_md_path = ai_ocr_dir / f"page_{pg:02d}.md"
        if (not force and pg in existing_corrected
                and existing_corrected[pg].strip()
                and page_md_path.exists()):
            tprint(f"  p{pg:02d} SKIP (exists)", worker=worker_id, level=2)
            continue

        # Budget check
        if cost_tracker and cost_tracker.would_exceed_budget():
            tprint(f"\n  BUDGET LIMIT: Stopping before page {pg}. "
                   f"{cost_tracker.summary()}", worker=worker_id, level=1)
            break

        tprint(f"  p{pg:02d} correcting ...", worker=worker_id, level=1)

        result = correct_page(
            ark_id, pg, actual_pages, newspaper, date, fname,
            system_prompt, api_key, rate_limiter, cost_tracker)

        if result["status"] == "ok":
            corrected_pages[pg] = result["text"]
            page_md_path.write_text(result["markdown"], encoding="utf-8")
            tprint(f"  p{pg:02d} ok  ({len(result['text'])} chars)",
                   worker=worker_id, level=1)
        elif result["status"] == "no_image":
            tprint(f"  p{pg:02d} SKIP (no image or OCR)",
                   worker=worker_id, level=1)
            corrected_pages[pg] = ""
        else:
            tprint(f"  p{pg:02d} FAILED: {result.get('error', 'unknown')}",
                   worker=worker_id, level=1)
            corrected_pages[pg] = (
                f"[CORRECTION FAILED: {result.get('error', 'unknown')}]")

        if delay > 0:
            time.sleep(delay)

    # Write corrected/ file
    out_lines = [header, ""]
    for pg in sorted(corrected_pages.keys()):
        out_lines.append(f"--- Page {pg} of {actual_pages} ---")
        out_lines.append(corrected_pages[pg])
        out_lines.append("")
    corr_path.write_text('\n'.join(out_lines), encoding="utf-8")
    tprint(f"  -> corrected/{fname}  ({corr_path.stat().st_size // 1024}KB)",
           worker=worker_id, level=1)

    # Article segmentation
    tprint(f"  Article segmentation ...", worker=worker_id, level=1)
    all_items = []
    for pg in sorted(corrected_pages.keys()):
        text = corrected_pages[pg]
        if not text.strip() or text.startswith("[CORRECTION FAILED"):
            continue
        items = segment_page(pg, text, api_key, rate_limiter, cost_tracker)
        all_items.extend(items)
        tprint(f"    p{pg:02d} -> {len(items)} item(s)",
               worker=worker_id, level=2)

    # Cross-page stitching
    if len(corrected_pages) > 1 and all_items:
        tprint(f"  Stitching across page boundaries ...",
               worker=worker_id, level=1)
        all_items = stitch_all_pages(
            all_items, api_key, rate_limiter, cost_tracker, worker_id)

    # Write article files
    n = write_article_files(issue, all_items, ark_dir)
    tprint(f"  -> articles/{ark_id}/  ({n} files)",
           worker=worker_id, level=1)

    return "ok"


# ============================================================================
# COMPILE — READABLE MARKDOWN OUTPUT
# ============================================================================

READABLE_DIR = None


def compute_confidence(raw_text: str) -> dict:
    """Compute confidence breakdown from a raw AI OCR page response."""
    stats = extract_stats(raw_text)
    if stats:
        return stats

    # Fall back: count characters from the markup directly
    # Parse gap tags and bucket by cnf value
    cnf_high = 0   # >= 0.80
    cnf_mid = 0    # 0.40 - 0.79
    cnf_low = 0    # < 0.40
    gap_pattern = re.compile(
        r'\{\{\s*gap\s*\|[^[]*?cnf="([^"]*)"[^[]*\[([^\]]*)\]\s*\}\}')
    for m in gap_pattern.finditer(raw_text):
        try:
            cnf = float(m.group(1))
        except (ValueError, TypeError):
            cnf = 0.0
        chars = len(m.group(2))
        if cnf >= 0.80:
            cnf_high += chars
        elif cnf >= 0.40:
            cnf_mid += chars
        else:
            cnf_low += chars

    clean = extract_clean_text(raw_text)
    total_chars = len(clean) if clean else 1
    gap_total = cnf_high + cnf_mid + cnf_low
    no_gap = max(0, total_chars - gap_total)

    return {
        "estimated_chars": total_chars,
        "chars_no_gap": no_gap,
        "high_confidence_pct": round(no_gap / total_chars * 100, 1)
                               if total_chars > 0 else 0,
        "chars_cnf_high": cnf_high,
        "chars_cnf_mid": cnf_mid,
        "chars_cnf_low": cnf_low,
    }


def compile_issue(issue: dict, config: dict, collection_dir: Path):
    """
    Compile a single readable markdown file from per-page AI OCR output.
    Reads ai_ocr/{ark_id}/page_NN.md files, strips markup, adds header.
    Output: readable/{issue_filename}.md
    """
    ark_id = issue["ark_id"]
    vol    = str(issue.get("volume", "?")).zfill(2)
    num    = str(issue.get("number", "?")).zfill(2)
    date   = re.sub(r"[^\w\-]", "-", issue.get("date", "unknown"))
    fname  = f"{ark_id}_vol{vol}_no{num}_{date}"
    newspaper = config.get("title_name", "")

    ai_ocr_dir = collection_dir / "ai_ocr" / ark_id
    readable_dir = collection_dir / "readable"
    readable_dir.mkdir(parents=True, exist_ok=True)

    if not ai_ocr_dir.exists():
        return None

    # Collect all page files
    page_files = sorted(ai_ocr_dir.glob("page_*.md"))
    if not page_files:
        return None

    total_pages = len(page_files)
    all_clean_pages = []
    total_stats = {
        "estimated_chars": 0,
        "chars_no_gap": 0,
        "chars_cnf_high": 0,
        "chars_cnf_mid": 0,
        "chars_cnf_low": 0,
    }

    for pf in page_files:
        raw = pf.read_text(encoding="utf-8", errors="replace")

        # Get confidence stats from this page
        page_stats = compute_confidence(raw)
        for k in total_stats:
            total_stats[k] += page_stats.get(k, 0)

        # Extract clean text
        clean = extract_clean_text(raw)
        pg_num = re.search(r'page_(\d+)', pf.stem)
        pg_label = int(pg_num.group(1)) if pg_num else 0

        all_clean_pages.append((pg_label, clean))

    # Compute overall confidence percentage
    est = total_stats["estimated_chars"]
    no_gap = total_stats["chars_no_gap"]
    pct = round(no_gap / est * 100, 1) if est > 0 else 0

    # Build the readable markdown
    today = datetime.now().strftime("%Y-%m-%d")
    issue_date = issue.get("date", "unknown")
    full_title = issue.get("full_title", newspaper)

    lines = [
        f"# {newspaper}",
        f"## {full_title}",
        "",
        f"**Date:** {issue_date}  ",
        f"**Volume:** {issue.get('volume', '?')}  "
        f"**Number:** {issue.get('number', '?')}  ",
        f"**Pages:** {total_pages}  ",
        f"**Source:** Portal to Texas History "
        f"([{ark_id}](https://texashistory.unt.edu/ark:/67531/{ark_id}/))  ",
        "",
        f"**Compiled:** {today}  ",
        f"**High-confidence text:** {pct}%  ",
        f"**Pipeline:** AI OCR v2.0 (Claude Vision)  ",
        "",
        "---",
        "",
    ]

    for pg_num, clean_text in sorted(all_clean_pages):
        lines.append(f"[---Page {pg_num}---]")
        lines.append("")
        if clean_text.strip():
            lines.append(clean_text)
        else:
            lines.append("*(page empty or unavailable)*")
        lines.append("")

    out_path = readable_dir / f"{fname}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


def compile_all(issues: list, config: dict, collection_dir: Path,
                resume: bool = True):
    """Compile readable markdown for all issues."""
    readable_dir = collection_dir / "readable"
    readable_dir.mkdir(parents=True, exist_ok=True)

    compiled = 0
    skipped = 0
    for issue in issues:
        ark_id = issue["ark_id"]
        vol = str(issue.get("volume", "?")).zfill(2)
        num = str(issue.get("number", "?")).zfill(2)
        date = re.sub(r"[^\w\-]", "-", issue.get("date", "unknown"))
        fname = f"{ark_id}_vol{vol}_no{num}_{date}.md"
        out_path = readable_dir / fname

        if resume and out_path.exists() and out_path.stat().st_size > 100:
            skipped += 1
            continue

        result = compile_issue(issue, config, collection_dir)
        if result:
            tprint(f"  {fname}  ({result.stat().st_size // 1024}KB)", level=1)
            compiled += 1
        else:
            tprint(f"  {fname}  (no ai_ocr data)", level=2)

    print(f"\nCompiled: {compiled}  Skipped: {skipped}  "
          f"Output: {readable_dir}/", flush=True)


# ============================================================================
# COST ESTIMATION DISPLAY
# ============================================================================

def show_cost_estimate(model, pages_to_process, budget):
    """Display pre-batch cost estimate. Returns estimated cost."""
    input_price, output_price = 3.0, 15.0  # Sonnet defaults
    if load_pricing:
        pricing = load_pricing()
        mp = pricing.get(model, {})
        input_price = mp.get("input", input_price)
        output_price = mp.get("output", output_price)

    total_in = pages_to_process * EST_INPUT_TOKENS_PER_PAGE
    total_out = pages_to_process * EST_OUTPUT_TOKENS_PER_PAGE
    est_cost = (total_in * input_price + total_out * output_price) / 1_000_000
    est_high = est_cost * 1.3

    print()
    print("=" * 60)
    print("  AI OCR CORRECTION -- COST ESTIMATE")
    print("=" * 60)
    print(f"  Model            : {model}")
    print(f"  Pages to process : {pages_to_process:,}")
    print(f"  Est. input/page  : ~{EST_INPUT_TOKENS_PER_PAGE:,} tokens")
    print(f"  Est. output/page : ~{EST_OUTPUT_TOKENS_PER_PAGE:,} tokens")
    print(f"  Input rate       : ${input_price:.2f}/MTok")
    print(f"  Output rate      : ${output_price:.2f}/MTok")
    print(f"  Est. total cost  : ${est_cost:.2f} - ${est_high:.2f}")
    if budget is not None:
        print(f"  Budget limit     : ${budget:.2f}")
        if est_high > budget:
            per_page = est_cost / max(pages_to_process, 1)
            affordable = int(budget / per_page) if per_page > 0 else 0
            print(f"  Budget covers    : ~{affordable} of "
                  f"{pages_to_process} pages")
    print("=" * 60)
    print()
    return est_cost


def show_revised_estimate(cost_tracker, pages_remaining, total_pages):
    """Show revised cost estimate after first issue."""
    est_remaining = cost_tracker.estimate_remaining(pages_remaining)
    avg = cost_tracker.avg_cost_per_page()

    print()
    print("-" * 60)
    print("  REVISED COST ESTIMATE (based on actual usage)")
    print("-" * 60)
    print(f"  Pages processed  : {cost_tracker.pages_processed}")
    print(f"  Actual cost/page : ${avg:.4f}")
    print(f"  Spent so far     : ${cost_tracker.total_cost:.2f}")
    print(f"  Pages remaining  : {pages_remaining}")
    print(f"  Est. remaining   : ${est_remaining:.2f}")
    print(f"  Est. total       : "
          f"${cost_tracker.total_cost + est_remaining:.2f}")
    if cost_tracker.budget is not None:
        remaining_budget = cost_tracker.budget - cost_tracker.total_cost
        print(f"  Budget remaining : ${remaining_budget:.2f}")
        if est_remaining > remaining_budget:
            affordable = int(remaining_budget / max(avg, 0.001))
            print(f"  Budget covers    : ~{affordable} more pages")
    print("-" * 60)
    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    p = argparse.ArgumentParser(
        description="UNT Archive -- AI OCR Correction Pipeline")
    p.add_argument("--config-path",    required=True)
    p.add_argument("--api-key",
                   default=os.environ.get("ANTHROPIC_API_KEY", ""))
    p.add_argument("--preload-images", action="store_true")
    p.add_argument("--workers",        type=int, default=4)
    p.add_argument("--resume",         action="store_true")
    p.add_argument("--force",          action="store_true",
                   help="Reprocess all pages, ignoring existing output")
    p.add_argument("--retry-failed",   action="store_true")
    p.add_argument("--ark",            default=None)
    p.add_argument("--date-from",      default=None)
    p.add_argument("--date-to",        default=None)
    p.add_argument("--delay",          type=float, default=1.0)
    p.add_argument("--api-workers",    type=int, default=3)
    p.add_argument("--serial",         action="store_true")
    p.add_argument("--tier",           default="default",
                   choices=["default", "build", "custom"])
    p.add_argument("--compile",        action="store_true",
                   help="Compile readable markdown from AI OCR output "
                        "(no API calls, run after --correct)")
    p.add_argument("--budget",         type=float, default=None,
                   help="Max dollar amount to spend (stops before exceeding)")
    p.add_argument("--logging",        type=int, default=1,
                   choices=[1, 2, 3, 4, 5],
                   help="Log verbosity: 1=progress 2=pages 3=api 4=detail "
                        "5=verbose")
    p.add_argument("--verbose",        action="store_true",
                   help="Shorthand for --logging 5")
    p.add_argument("--yes",            action="store_true",
                   help="Skip cost confirmation prompt")
    # Accept but ignore these (passed by orchestrator)
    p.add_argument("--issue-delay",    type=float, default=None)
    p.add_argument("--max-output-tokens", type=int, default=None)
    args = p.parse_args()

    global LOG_LEVEL
    LOG_LEVEL = 5 if args.verbose else args.logging

    config_path = Path(args.config_path)
    if not config_path.exists():
        sys.exit(f"Config not found: {config_path}")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    collection_dir = config_path.parent
    init_paths(collection_dir)

    # Load global config
    global_config_path = Path(__file__).parent / "config.json"
    global_config = {}
    if global_config_path.exists():
        try:
            global_config = json.loads(
                global_config_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Initialize skill directory from global config
    init_skill_dir(global_config)

    global CLAUDE_MODEL
    if config.get("claude_model"):
        CLAUDE_MODEL = config["claude_model"]
    elif global_config.get("claude_model"):
        CLAUDE_MODEL = global_config["claude_model"]

    # API key resolution
    api_key = (args.api_key
               or os.environ.get("ANTHROPIC_API_KEY", "")
               or global_config.get("anthropic_api_key", "")
               or config.get("anthropic_api_key", ""))
    if not args.preload_images and not args.compile and not api_key:
        sys.exit("Error: API key required. Set in config.json, "
                 "ANTHROPIC_API_KEY env var, or --api-key flag.")

    # Load issue index
    index_path = METADATA_DIR / "all_issues.json"
    if not index_path.exists():
        sys.exit(f"No issue index at {index_path}. Run --discover first.")
    with open(index_path, encoding="utf-8") as f:
        all_issues = json.load(f)

    issues = all_issues
    if args.ark:
        issues = [i for i in issues if i["ark_id"] == args.ark]
    if args.date_from:
        issues = [i for i in issues if i.get("date", "") >= args.date_from]
    if args.date_to:
        issues = [i for i in issues if i.get("date", "") <= args.date_to]

    print(f"Collection : {config['title_name']}", flush=True)
    print(f"Issues     : {len(issues)}", flush=True)
    print(f"Model      : {CLAUDE_MODEL}", flush=True)
    print(f"Pipeline   : AI-only (Claude Vision)", flush=True)

    # Check image cache
    cached = sum(1 for i in issues
                 for pg in range(1, int(i.get("pages", 8)) + 1)
                 if is_valid_cached_image(
                     local_image_path(i["ark_id"], pg)))
    total_pg = sum(int(i.get("pages", 8)) for i in issues)
    cache_ok = cached == total_pg
    print(f"Images     : {cached}/{total_pg} "
          f"{'ok' if cache_ok else '(run --preload-images)'}",
          flush=True)

    if args.preload_images:
        preload_images(issues, resume=True,
                       retry_failed=args.retry_failed,
                       workers=args.workers)
        return

    if args.compile:
        print(f"\nCompiling readable markdown from AI OCR output ...",
              flush=True)
        compile_all(issues, config, collection_dir,
                    resume=not args.force)
        return

    # Count pages to process (respecting --resume/--force)
    pages_to_process = 0
    for issue in issues:
        ark_id = issue["ark_id"]
        vol = str(issue.get("volume", "?")).zfill(2)
        num = str(issue.get("number", "?")).zfill(2)
        date = re.sub(r"[^\w\-]", "-", issue.get("date", "unknown"))
        fname = f"{ark_id}_vol{vol}_no{num}_{date}.txt"
        corr_path = CORRECTED_DIR / fname
        ark_dir = ARTICLES_DIR / ark_id

        if (args.resume and not args.force
                and corr_path.exists()
                and corr_path.stat().st_size > 500):
            if ark_dir.exists() and any(ark_dir.glob("*_art*.txt")):
                continue
        pages_to_process += int(issue.get("pages", 8))

    if pages_to_process == 0:
        print("\nNothing to process -- all files already complete.")
        return

    # Cost estimate and confirmation
    show_cost_estimate(CLAUDE_MODEL, pages_to_process, args.budget)

    if not args.yes:
        confirm = input(
            f"Proceed with AI OCR correction? "
            f"({pages_to_process} pages) [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Cancelled.")
            return

    # Initialize tracking
    system_prompt = build_system_prompt(config)
    rate_limiter = (limiter_from_tier(args.tier)
                    if ClaudeRateLimiter else None)
    cost_tracker = CostTracker(CLAUDE_MODEL, budget=args.budget)

    log = []
    log_lock = threading.Lock()
    ctr = {"ok": 0, "skipped": 0, "err": 0}

    first_issue_done = False

    for idx, issue in enumerate(issues):
        ark_id = issue["ark_id"]
        tprint(f"\n{'=' * 60}", level=1)
        tprint(f"[{idx + 1}/{len(issues)}] {ark_id}  "
               f"Vol.{issue.get('volume', '?')} "
               f"No.{issue.get('number', '?')}  "
               f"{issue.get('date', '')}", level=1)

        # Budget check before starting issue
        if cost_tracker.would_exceed_budget():
            tprint(f"\nBUDGET LIMIT REACHED. {cost_tracker.summary()}",
                   level=1)
            break

        status = process_issue(
            issue, api_key, system_prompt,
            args.delay, args.resume, args.force,
            rate_limiter=rate_limiter,
            cost_tracker=cost_tracker,
            worker_id="")

        with log_lock:
            log.append({"ark_id": ark_id, "status": status})
            (CORRECTED_DIR / "correction_log.json").write_text(
                json.dumps(log, indent=2), encoding="utf-8")

        if status == "ok":
            ctr["ok"] += 1
        elif status == "skipped":
            ctr["skipped"] += 1
        else:
            ctr["err"] += 1

        # After first completed issue: show revised estimate
        if (not first_issue_done and status == "ok"
                and cost_tracker.pages_processed > 0):
            first_issue_done = True
            pages_done = cost_tracker.pages_processed
            pages_remaining = pages_to_process - pages_done
            if pages_remaining > 0:
                show_revised_estimate(
                    cost_tracker, pages_remaining, pages_to_process)

                # Warn if budget insufficient
                if cost_tracker.budget is not None:
                    est_rem = cost_tracker.estimate_remaining(pages_remaining)
                    bud_rem = (cost_tracker.budget
                               - cost_tracker.total_cost)
                    if est_rem > bud_rem * 1.1:
                        avg = cost_tracker.avg_cost_per_page()
                        affordable = int(bud_rem / max(avg, 0.001))
                        print(f"  WARNING: Budget may not cover all "
                              f"remaining pages. ~{affordable} more "
                              f"pages affordable.")
                        if not args.yes:
                            confirm = input(
                                "Continue? [y/N]: ").strip().lower()
                            if confirm not in ("y", "yes"):
                                print("Stopped by user.")
                                break

    # Final summary
    if rate_limiter:
        tprint(f"\nRate limiter: {rate_limiter.status_line()}", level=1)
    tprint(f"\n{'=' * 60}", level=1)
    tprint(f"Complete: {ctr['ok']}  Skipped: {ctr['skipped']}  "
           f"Errors: {ctr['err']}", level=1)
    tprint(f"Cost: {cost_tracker.summary()}", level=1)
    tprint(f"Corrected: {CORRECTED_DIR}", level=1)
    tprint(f"AI OCR:    {AI_OCR_DIR}", level=1)
    tprint(f"Articles:  {ARTICLES_DIR}", level=1)


if __name__ == "__main__":
    main()
