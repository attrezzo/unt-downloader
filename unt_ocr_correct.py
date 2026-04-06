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
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
import urllib.request, urllib.error
import signal

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
# INTERRUPT HANDLING
# ============================================================================

_interrupted = False


def _handle_sigint(sig, frame):
    """Handle Ctrl+C. First press sets flag, second press force-exits."""
    global _interrupted
    if _interrupted:
        # Second Ctrl+C — force exit immediately
        print("\n  Force quit.", flush=True)
        # os._exit works even when threads are blocked
        os._exit(1)
    _interrupted = True
    print("\n  Ctrl+C — stopping after current API calls finish...",
          flush=True)


# Install handler — works on Windows and Unix
signal.signal(signal.SIGINT, _handle_sigint)


def run_parallel(fn, items, max_workers=3, label="items"):
    """Run fn over items with a thread pool. Handles Ctrl+C cleanly.
    fn(item) -> result. Returns list of (item, result) pairs.

    On Windows, Ctrl+C can't interrupt blocked threads (urllib etc).
    We poll futures with a short timeout so the main thread can check
    the _interrupted flag regularly."""
    global _interrupted
    _interrupted = False
    results = []

    ex = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {ex.submit(fn, item): item for item in items}
        pending = set(futures.keys())

        while pending and not _interrupted:
            # Poll with short timeout so Ctrl+C can fire between polls
            done_batch = set()
            for fut in list(pending):
                if fut.done():
                    done_batch.add(fut)
            if not done_batch:
                # Nothing ready — sleep briefly to let signals fire
                try:
                    time.sleep(0.3)
                except KeyboardInterrupt:
                    _interrupted = True
                    break
                continue
            for fut in done_batch:
                pending.discard(fut)
                try:
                    results.append((futures[fut], fut.result(timeout=0)))
                except Exception as e:
                    results.append((futures[fut], e))
    except KeyboardInterrupt:
        _interrupted = True

    if _interrupted:
        for f in futures:
            f.cancel()
        print(f"  Stopped. {len(results)}/{len(items)} "
              f"{label} completed.", flush=True)

    ex.shutdown(wait=False)
    return results


# ============================================================================
# CONSTANTS
# ============================================================================

UNT_BASE      = "https://texashistory.unt.edu"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
ILLEGIBLE     = "[unleserlich]"  # deprecated — kept only for parsing legacy files

# Default model tiers (user-selectable via --model-* flags)
MODEL_INITIAL = "claude-haiku-4-5"     # Pass 1-2: image + transcription (cheap)
MODEL_RESOLVE = "claude-sonnet-4-6"    # Pass 3: cross-reference + guess (text-only)
MODEL_REFINE  = "claude-opus-4-6"      # Future refinement of low-cnf gaps

# Token estimates
EST_INPUT_TOKENS_PASS12  = 66_000  # image + system prompt + OCR text
EST_OUTPUT_TOKENS_PASS12 = 4_500   # full page text with gap markers
EST_INPUT_TOKENS_PASS3   = 10_000  # text-only: Pass 1-2 output + OCR + prompt
EST_OUTPUT_TOKENS_PASS3  = 4_500   # same text with gaps filled
MAX_OUTPUT_TOKENS        = 16_000

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


# ============================================================================
# LOGGING & STATUS DISPLAY
# ============================================================================

import logging as _logging_mod
from collections import deque

LOG_LEVEL = 1
_file_logger = None
_activity_log = None


class ActivityLog:
    """Thread-safe rolling log of recent worker activity for CLI display."""

    def __init__(self, max_items: int = 50):
        self._lock = threading.Lock()
        self._items = deque(maxlen=max_items)
        self._total = 0
        self._ok = 0
        self._err = 0
        self._skip = 0
        self._start_time = time.monotonic()

    def add(self, msg: str, status: str = "info"):
        with self._lock:
            self._items.appendleft((time.monotonic(), msg, status))
            self._total += 1
            if status == "ok":
                self._ok += 1
            elif status == "error":
                self._err += 1
            elif status == "skip":
                self._skip += 1

    def recent(self, n: int = 5) -> list:
        with self._lock:
            return list(self._items)[:n]

    def stats(self) -> dict:
        with self._lock:
            elapsed = time.monotonic() - self._start_time
            rate = self._ok / (elapsed / 60) if elapsed > 0 else 0
            return {
                "total": self._total, "ok": self._ok,
                "err": self._err, "skip": self._skip,
                "elapsed": elapsed, "rate": rate,
            }


def init_logging(collection_dir: Path, log_level: int):
    """Initialize file logger and activity log."""
    global _file_logger, _activity_log, LOG_LEVEL
    LOG_LEVEL = log_level
    _activity_log = ActivityLog()

    if log_level >= 2:
        log_dir = collection_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"ocr_{ts}.log"
        _file_logger = _logging_mod.getLogger("unt_ocr")
        _file_logger.setLevel(_logging_mod.DEBUG)
        handler = _logging_mod.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(_logging_mod.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S"))
        _file_logger.addHandler(handler)
        _file_logger.info(f"Log started: level={log_level}")
        print(f"  Log file: {log_path}")


def log_debug(msg: str, level: int = 2):
    """Write to debug log file only (if logging enabled)."""
    if _file_logger and level <= LOG_LEVEL:
        _file_logger.debug(msg)


def log_event(msg: str, status: str = "info"):
    """Record an event: always prints to stdout, adds to activity log,
    and writes to file log if enabled."""
    icon = {"ok": "+", "error": "X", "skip": "-"}.get(status, " ")
    print(f"  {icon} {msg}", flush=True)
    if _activity_log:
        _activity_log.add(msg, status)
    if _file_logger:
        _file_logger.info(msg)


def tprint(*args, worker: str = "", level: int = 1, **kwargs):
    """Print to stdout (level 1) and/or file log (all levels).
    Level 1 always prints. Level 2+ only prints to file."""
    msg = ' '.join(str(a) for a in args)
    prefix = f"[{worker}] " if worker else ""
    full = f"{prefix}{msg}"
    # Level 1: always show on stdout
    if level <= 1:
        print(f"  {full}", flush=True)
        if _activity_log:
            _activity_log.add(msg, "info")
    # All levels: write to file if logging enabled
    if _file_logger and level <= LOG_LEVEL:
        _file_logger.debug(full)


def print_status(cost_tracker=None, step_name="OCR Correction",
                 progress_current=0, progress_total=0):
    """Print a clean status block to stdout."""
    if not _activity_log:
        return

    s = _activity_log.stats()

    lines = []
    lines.append("")
    lines.append(f"  {step_name}")

    if progress_total > 0:
        pct = progress_current / progress_total * 100
        bar_len = 30
        filled = int(bar_len * progress_current / progress_total)
        bar = "#" * filled + "-" * (bar_len - filled)
        lines.append(f"  [{bar}] {progress_current}/{progress_total} "
                     f"({pct:.0f}%)")

    cost_str = ""
    if cost_tracker:
        cost_str = f" | Cost: ${cost_tracker.total_cost:.2f}"
        if cost_tracker.budget:
            cost_str += f"/${cost_tracker.budget:.2f}"

    rate_str = f"{s['rate']:.1f}/min" if s['rate'] > 0 else "--"
    lines.append(f"  Done: {s['ok']}  Skip: {s['skip']}  "
                 f"Err: {s['err']}  Rate: {rate_str}{cost_str}")
    lines.append("")

    recent = _activity_log.recent(5)
    now = time.monotonic()
    if recent:
        lines.append("  Recent:")
        for ts, msg, status in recent:
            ago = now - ts
            if ago < 60:
                ago_str = f"{ago:.0f}s ago"
            else:
                ago_str = f"{ago/60:.0f}m ago"
            icon = {"ok": "+", "error": "X", "skip": "-"}.get(status, " ")
            lines.append(f"    {icon} {msg:<55} ({ago_str})")
    lines.append("")

    print("\n".join(lines), flush=True)


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
SKILL_DIR      = Path(__file__).parent / "initial-ocr skill"

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
    """Track actual API costs during a batch run. Thread-safe."""

    REVISE_AFTER_PAGES = 5  # show revised estimate after this many pages

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
        self._revised_shown = False
        self._budget_abort = False  # set True if estimate > budget

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
                    (EST_INPUT_TOKENS_PASS12 + EST_INPUT_TOKENS_PASS3) *
                    self.input_price +
                    (EST_OUTPUT_TOKENS_PASS12 + EST_OUTPUT_TOKENS_PASS3) *
                    self.output_price
                ) / 1_000_000
            avg_in = self.total_input_tokens / self.pages_processed
            avg_out = self.total_output_tokens / self.pages_processed
            return pages_left * (
                avg_in * self.input_price + avg_out * self.output_price
            ) / 1_000_000

    def would_exceed_budget(self) -> bool:
        """Check if processing one more page would likely exceed budget."""
        if self.budget is None:
            return False
        if self._budget_abort:
            return True
        with self._lock:
            if self.pages_processed == 0:
                avg_cost = (
                    (EST_INPUT_TOKENS_PASS12 + EST_INPUT_TOKENS_PASS3) *
                    self.input_price +
                    (EST_OUTPUT_TOKENS_PASS12 + EST_OUTPUT_TOKENS_PASS3) *
                    self.output_price) / 1_000_000
            else:
                avg_cost = self.total_cost / self.pages_processed
            return (self.total_cost + avg_cost) > self.budget

    def avg_cost_per_page(self) -> float:
        with self._lock:
            if self.pages_processed == 0:
                return (
                    (EST_INPUT_TOKENS_PASS12 + EST_INPUT_TOKENS_PASS3) *
                    self.input_price +
                    (EST_OUTPUT_TOKENS_PASS12 + EST_OUTPUT_TOKENS_PASS3) *
                    self.output_price) / 1_000_000
            return self.total_cost / self.pages_processed

    def should_show_revised(self) -> bool:
        """Returns True once, after REVISE_AFTER_PAGES pages processed."""
        with self._lock:
            if (not self._revised_shown
                    and self.pages_processed >= self.REVISE_AFTER_PAGES):
                self._revised_shown = True
                return True
            return False

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

def _claude_call_standard(req_data, api_key, timeout=300):
    """Standard (non-streaming) API call. Returns parsed JSON dict."""
    req = urllib.request.Request(
        ANTHROPIC_API, data=req_data,
        headers={"Content-Type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _claude_call_streaming(req_data, api_key, label, timeout=300):
    """Streaming API call. Reads SSE events, shows progress on stdout.

    LOG_LEVEL 4: stdout shows token count every 3s (activity indicator)
    LOG_LEVEL 5: stdout shows token count every 2s, file log gets every
                 SSE event type, full usage details, and response text
    """
    payload = json.loads(req_data)
    payload["stream"] = True
    stream_data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        ANTHROPIC_API, data=stream_data,
        headers={"Content-Type": "application/json",
                 "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
        method="POST")
    resp = urllib.request.urlopen(req, timeout=timeout)

    text_parts = []
    usage = {}
    out_tokens = 0
    event_count = 0
    last_report = time.monotonic()
    t_start = time.monotonic()
    verbose = LOG_LEVEL >= 5
    report_interval = 2.0 if verbose else 3.0

    try:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str == "[DONE]":
                if verbose:
                    log_debug(f"{label} SSE: [DONE] after "
                              f"{event_count} events", level=5)
                break
            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")
            event_count += 1

            if verbose:
                log_debug(f"{label} SSE #{event_count}: {etype}", level=5)

            if etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    chunk = delta.get("text", "")
                    text_parts.append(chunk)
                    out_tokens += len(chunk) // 4 + 1

                    now = time.monotonic()
                    if now - last_report >= report_interval:
                        elapsed = now - t_start
                        print(f"    {label} generating... "
                              f"~{out_tokens} tok  {elapsed:.0f}s",
                              flush=True)
                        last_report = now

            elif etype == "message_start":
                msg = event.get("message", {})
                u = msg.get("usage", {})
                if u:
                    usage["input_tokens"] = u.get("input_tokens", 0)
                    if verbose:
                        log_debug(f"{label} input_tokens="
                                  f"{usage['input_tokens']}", level=5)

            elif etype == "message_delta":
                u = event.get("usage", {})
                if u:
                    usage["output_tokens"] = u.get("output_tokens", 0)

            elif etype == "error":
                err = event.get("error", {})
                log_event(f"{label} stream error: "
                          f"{err.get('message', event)}", "error")
                if verbose:
                    log_debug(f"{label} error event: "
                              f"{json.dumps(event)}", level=5)

    finally:
        resp.close()

    text = "".join(text_parts).strip()

    # Level 5: log the full response text for debugging
    if verbose and text:
        log_debug(f"{label} response text ({len(text)} chars):\n"
                  f"{text[:2000]}{'...[truncated]' if len(text) > 2000 else ''}",
                  level=5)

    return text, usage


def claude_api_call(payload: dict, api_key: str,
                    rate_limiter=None, est_tokens: int = 8000,
                    cost_tracker=None, label: str = ""):
    """Make a Claude API call with retry/rate-limit. Returns (text, usage).
    Uses streaming when LOG_LEVEL >= 4 for progress feedback."""
    model = payload.get("model", "?")
    use_streaming = LOG_LEVEL >= 4
    log_debug(f"API call: model={model} max_tokens={payload.get('max_tokens')} "
              f"est={est_tokens} stream={use_streaming} label={label}",
              level=3)
    req_data = json.dumps(payload).encode("utf-8")
    req_mb = len(req_data) / 1_048_576

    # Level 5: log payload structure for debugging
    if LOG_LEVEL >= 5:
        sys_len = len(payload.get("system", ""))
        msgs = payload.get("messages", [])
        msg_summary = []
        for m in msgs:
            role = m.get("role", "?")
            content = m.get("content", "")
            if isinstance(content, str):
                msg_summary.append(f"{role}:{len(content)}chars")
            elif isinstance(content, list):
                parts = []
                for c in content:
                    if c.get("type") == "image":
                        parts.append("image")
                    elif c.get("type") == "text":
                        parts.append(f"text:{len(c.get('text',''))}chars")
                msg_summary.append(f"{role}:[{','.join(parts)}]")
        log_debug(f"{label} payload: {req_mb:.2f}MB  system={sys_len}chars  "
                  f"messages={' '.join(msg_summary)}", level=5)

    if rate_limiter:
        t_wait = time.monotonic()
        if label:
            log_event(f"{label} waiting for rate limiter...")
        rate_limiter.acquire(estimated_tokens=est_tokens)
        waited = time.monotonic() - t_wait
        if waited > 1.0 and label:
            log_event(f"{label} rate limiter wait: {waited:.0f}s")

    for attempt in range(1, 4):
        t0 = time.monotonic()
        if label:
            mode = " (streaming)" if use_streaming else ""
            log_event(f"{label} sending {req_mb:.1f}MB to {model}{mode}...")
        try:
            if use_streaming:
                text, usage = _claude_call_streaming(
                    req_data, api_key, label)
            else:
                result = _claude_call_standard(req_data, api_key)
                usage = result.get("usage", {})
                text = ""
                for block in result.get("content", []):
                    if block.get("type") == "text":
                        text = block["text"].strip()
                        break

            elapsed = time.monotonic() - t0
            in_tok = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            if rate_limiter:
                rate_limiter.record_usage(
                    input_tokens=in_tok, output_tokens=out_tok)
            if cost_tracker:
                cost_tracker.record(in_tok, out_tok)
            if label:
                log_event(f"{label} done  {in_tok:,}in+{out_tok:,}out  "
                          f"{elapsed:.1f}s", "ok")
            log_debug(f"API response: {in_tok}in+{out_tok}out "
                      f"{elapsed:.1f}s model={model}", level=3)
            return text, usage
        except urllib.error.HTTPError as e:
            elapsed = time.monotonic() - t0
            if e.code in (429, 503, 529):
                wait = 30 * attempt
                log_event(f"{label} rate limit {e.code} after "
                          f"{elapsed:.0f}s, retry in {wait}s...")
                time.sleep(wait)
            else:
                log_event(f"{label} HTTP {e.code} after {elapsed:.0f}s",
                          "error")
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

def _build_collection_header(config: dict) -> str:
    """Shared collection context for all prompts."""
    title      = config.get("title_name", "")
    publisher  = config.get("publisher", "")
    location   = config.get("pub_location", "Texas")
    date_range = config.get("date_range", "")
    language   = config.get("language", "German")
    typeface   = config.get("typeface", "Fraktur")
    source     = config.get("source_medium", "microfilm")
    lccn       = config.get("lccn", "")
    ctx = ""
    for key, label in [("community_desc", "COMMUNITY"),
                       ("historical_context", "HISTORY"),
                       ("subject_notes", "SUBJECTS"),
                       ("place_names", "PLACE NAMES (preserve exactly)"),
                       ("organizations", "ORGANIZATIONS (preserve exactly)")]:
        val = config.get(key, "")
        if val:
            ctx += f"\n{label}: {val}"
    return (f"COLLECTION: {title}\n"
            f"Publisher: {publisher} | Location: {location} | "
            f"Period: {date_range}\n"
            f"Language: {language} | Typeface: {typeface} | Source: {source}\n"
            f"{'LCCN: ' + lccn if lccn else ''}"
            f"{ctx}")


def build_pass12_prompt(config: dict) -> str:
    """System prompt for Pass 1-2: image transcription + gap inventory.
    Sent with the page image to the initial model (default: Haiku)."""
    header = _build_collection_header(config)
    fraktur_errors = load_reference("fraktur-errors.md")
    texas_german   = load_reference("texas-german.md")

    return f"""You are an expert OCR specialist for 19th-century German Fraktur
newspaper text scanned from microfilm.

{header}

REFERENCE: FRAKTUR ERROR PATTERNS
===================================
{fraktur_errors}

REFERENCE: TEXAS GERMAN VOCABULARY & ORTHOGRAPHY
==================================================
{texas_german}

YOUR TASK: PASS 1 + PASS 2 (transcription and gap inventory)
==============================================================

Read the newspaper page image. Produce a transcription where most text is
plain, high-confidence output. Mark uncertain regions as gaps.

PASS 1 - DIRECT FRAKTUR OCR (high-confidence extraction):
1. Identify layout: masthead, column count, center features, damage
2. Read each section: masthead, center features, columns left-to-right
3. Transcribe Fraktur to Latin characters
4. Apply Fraktur error corrections as you read (Tier 1 aggressively,
   Tiers 2-5 with context)
5. Confident text: write directly, no tags. This should be most of the page.
6. Illegible/uncertain text: mark with a gap. Do NOT guess:
   {{{{ gap | est=NN | imgbbox="x,y,w,h" }}}}
   est = approximate char count (Fraktur is proportional — narrow chars
   like l,i,t take ~30-40% the width of w,M,W, so treat est as rough).
   imgbbox = pixel bounding box (be generous).
7. Images/illustrations: {{{{ Img | bbox="x,y,w,h" | desc="..." }}}}
8. Articles: {{{{ Column001 }}}} ... {{{{ /Column }}}}
9. Advertisements: {{{{ Ad001 }}}} ... {{{{ /Ad }}}}
10. Number Column/Ad tags sequentially (001, 002, 003...)
11. Headlines: ## | Subheads: ### | Datelines: **bold**
12. Do NOT correct Texas German dialect or pre-1901 spellings
13. Do NOT translate English loanwords

PASS 2 - GAP INVENTORY (observation only):
For each gap, re-examine the image. Record what you see. Do NOT guess.
Update each gap with fragments:
   {{{{ gap | est=NN | imgbbox="x,y,w,h" | fragments="visible_text" }}}}

OUTPUT FORMAT:
LAYOUT: <one-line description>
COLUMNS: <number>
DAMAGE: <notes or "none">

---

<transcribed text with gap markers — mostly plain text, gaps for uncertain regions>

---

STATS:
- estimated_chars: <N>
- chars_no_gap: <N>
- total_gaps: <N>

RULES:
- NEVER guess in gaps — that happens in a separate pass
- Preserve Texas German, period spellings, English loanwords as-is
- Watch for column interleaving and mark with
  <!-- {{{{ column_break | from=N | to=M }}}} -->"""


def build_pass3_prompt(config: dict) -> str:
    """System prompt for Pass 3: cross-reference, guess, confidence.
    Text-only call (no image needed) to the resolve model (default: Sonnet)."""
    header = _build_collection_header(config)
    fraktur_errors = load_reference("fraktur-errors.md")
    texas_german   = load_reference("texas-german.md")
    markup_spec    = load_reference("markup-spec.md")

    return f"""You are an expert in 19th-century German Fraktur OCR correction.

{header}

REFERENCE: FRAKTUR ERROR PATTERNS
===================================
{fraktur_errors}

REFERENCE: TEXAS GERMAN VOCABULARY & ORTHOGRAPHY
==================================================
{texas_german}

REFERENCE: MARKUP SPECIFICATION
================================
{markup_spec}

YOUR TASK: PASS 3 (cross-reference, guess, and confidence)
============================================================

You receive transcribed text from Pass 1-2 (with {{{{ gap }}}} markers for
uncertain regions) and the raw ABBYY/portal OCR text for the same page.

For every gap, produce a best guess and assign a confidence score.

INSTRUCTIONS:
1. For each {{{{ gap }}}} marker, cross-reference:
   - The fragments field (partial letterforms from the image)
   - The corresponding region in the ABBYY/portal OCR text
   - Surrounding context in the transcription
   - Your knowledge of 1890s German, Texas German dialect, article topic
2. Apply the Fraktur error correction table to decode garbled OCR fragments
3. Assign a confidence score and produce your best guess. Remember est
   is approximate (Fraktur proportional — l is ~30% width of W). Prefer
   linguistic sense over exact character count match.

   cnf >= 0.95: PROMOTE TO PLAIN TEXT. Remove the gap tag entirely.
     Write the text as untagged output.
   cnf 0.80-0.94: keep gap, add status=auto-resolved:
     {{{{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX" | status=auto-resolved | fragments="..." | region_ocr="raw" [guess] }}}}
   cnf < 0.80: keep gap, open for refinement:
     {{{{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX" | fragments="..." | region_ocr="raw" [guess] }}}}

4. region_ocr MUST contain the exact raw OCR text, uncorrected
5. Every remaining gap MUST have [guess] and cnf. Even cnf="0.00" with a
   wild guess is better than nothing.

cnf scale: 0.95-0.99=promote to plain, 0.80-0.94=auto-resolved,
0.70-0.79=moderate, 0.40-0.69=low, 0.01-0.39=speculative,
0.00=pure context guess.

OUTPUT: Return the complete text with gaps resolved. Use the same format
as the input (LAYOUT/COLUMNS/DAMAGE header, --- delimiters, STATS block).
Update the STATS to reflect resolved gaps.

RULES:
- Preserve Texas German, period spellings, English loanwords
- Do NOT translate
- Do NOT modernize orthography"""


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
                 pass12_prompt, pass3_prompt, api_key,
                 rate_limiter=None, cost_tracker=None):
    """
    Correct a single page using tiered Claude models.

    Call 1 (MODEL_INITIAL, e.g. Haiku): Image + Pass 1-2 prompt
      → transcription with {{ gap }} markers (no guesses)
    Call 2 (MODEL_RESOLVE, e.g. Sonnet): Pass 1-2 text + OCR → Pass 3
      → gaps resolved with guesses, cnf, region_ocr (text-only, no image)

    Returns dict: text, markdown, stats, status.
    """
    abbyy_text = get_abbyy_page_text(issue_fname, page_num)
    portal_ocr = get_portal_ocr_text(issue_fname, page_num)

    # ── Call 1: Pass 1-2 (image + transcription) ─────────────────────────
    content = build_page_prompt(
        ark_id, page_num, total_pages, newspaper, date,
        abbyy_text=abbyy_text, portal_ocr=portal_ocr)

    has_image = any(c.get("type") == "image" for c in content)
    if not has_image and not abbyy_text and not portal_ocr:
        return {"text": "", "markdown": "", "stats": {}, "status": "no_image"}

    # For rate limiter: use output token estimate, not full input.
    # Image tokens dominate input but don't consume the TPM rate window
    # the same way text generation does. Using full input (66k) with
    # default tier (40k TPM) would block 60+ seconds per page.
    est = EST_OUTPUT_TOKENS_PASS12 + 2000 if has_image else 5_000

    p_label = f"p{page_num:02d} pass1-2"
    try:
        pass12_raw, _ = claude_api_call(
            {"model": MODEL_INITIAL, "max_tokens": MAX_OUTPUT_TOKENS,
             "system": pass12_prompt,
             "messages": [{"role": "user", "content": content}]},
            api_key, rate_limiter, est_tokens=est,
            cost_tracker=cost_tracker, label=p_label)
    except Exception as e:
        log_event(f"p{page_num:02d} pass 1-2 FAILED  {e}", "error")
        return {"text": "", "markdown": "", "stats": {},
                "status": "failed", "error": str(e)}

    if not pass12_raw.strip():
        log_event(f"p{page_num:02d} pass 1-2 empty response", "error")
        return {"text": "", "markdown": "", "stats": {},
                "status": "failed", "error": "empty pass 1-2 response"}

    # ── Call 2: Pass 3 (text-only cross-reference) ───────────────────────
    ref_ocr = abbyy_text or portal_ocr or ""
    pass3_user = (
        f"PASS 1-2 TRANSCRIPTION (with gap markers):\n"
        f"```\n{pass12_raw}\n```\n\n")
    if ref_ocr:
        ocr_label = "ABBYY" if abbyy_text else "Portal"
        pass3_user += (
            f"{ocr_label} OCR TEXT (raw, for cross-referencing gaps):\n"
            f"```\n{ref_ocr[:30000]}\n```\n\n")
    pass3_user += (
        "Resolve all gaps: add [guess], cnf, region_ocr. "
        "Promote cnf >= 0.95 to plain text.")

    p3_label = f"p{page_num:02d} pass3"
    try:
        raw, _ = claude_api_call(
            {"model": MODEL_RESOLVE, "max_tokens": MAX_OUTPUT_TOKENS,
             "system": pass3_prompt,
             "messages": [{"role": "user", "content": pass3_user}]},
            api_key, rate_limiter, est_tokens=EST_INPUT_TOKENS_PASS3,
            cost_tracker=cost_tracker, label=p3_label)
    except Exception as e:
        log_event(f"p{page_num:02d} pass 3 FAILED  {e} (using pass 1-2)",
                  "error")
        raw = pass12_raw

    if not raw.strip():
        raw = pass12_raw

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
    header += f"- Model: {MODEL_INITIAL} (pass 1-2) + {MODEL_RESOLVE} (pass 3)\n"
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
        {"model": MODEL_RESOLVE, "max_tokens": 4000,
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
        {"model": MODEL_RESOLVE, "max_tokens": 100,
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

def process_issue(issue, api_key, pass12_prompt, pass3_prompt, delay,
                  resume, force, rate_limiter=None,
                  cost_tracker=None, worker_id="", api_workers=3):
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

    # Build list of pages that need processing
    pages_todo = []
    for pg in range(1, actual_pages + 1):
        page_md_path = ai_ocr_dir / f"page_{pg:02d}.md"
        if (not force and pg in existing_corrected
                and existing_corrected[pg].strip()
                and page_md_path.exists()):
            tprint(f"  p{pg:02d} SKIP (exists)", worker=worker_id, level=2)
            continue
        pages_todo.append(pg)

    if not pages_todo:
        tprint(f"  All pages already corrected", worker=worker_id, level=1)
    else:
        def _correct_one(pg):
            """Correct a single page (runs in thread pool)."""
            if _interrupted:
                return pg, {"status": "interrupted", "text": ""}
            if cost_tracker and cost_tracker.would_exceed_budget():
                return pg, {"status": "budget", "text": ""}
            result = correct_page(
                ark_id, pg, actual_pages, newspaper, date, fname,
                pass12_prompt, pass3_prompt, api_key,
                rate_limiter, cost_tracker)
            return pg, result

        pages_done = 0
        page_results = run_parallel(
            _correct_one, pages_todo,
            max_workers=api_workers, label="pages")

        for pg, result in page_results:
            if isinstance(result, Exception):
                log_event(f"p{pg:02d} {ark_id}  {result}", "error")
                corrected_pages[pg] = (
                    f"[CORRECTION FAILED: {result}]")
                continue
            # result is (pg, dict) from _correct_one
            _, res = result
            pages_done += 1
            page_md_path = ai_ocr_dir / f"page_{pg:02d}.md"
            if res["status"] == "ok":
                corrected_pages[pg] = res["text"]
                page_md_path.write_text(
                    res["markdown"], encoding="utf-8")
                log_event(
                    f"p{pg:02d} COMPLETE  {ark_id}  "
                    f"{len(res['text'])} chars", "ok")
            elif res["status"] == "no_image":
                log_event(f"p{pg:02d} {ark_id}  no image", "skip")
                corrected_pages[pg] = ""
            elif res["status"] in ("budget", "interrupted"):
                log_event(f"p{pg:02d} {ark_id}  {res['status']}", "skip")
            else:
                log_event(
                    f"p{pg:02d} {ark_id}  "
                    f"{res.get('error', 'unknown')}", "error")
                corrected_pages[pg] = (
                    f"[CORRECTION FAILED: "
                    f"{res.get('error', 'unknown')}]")

            print_status(cost_tracker,
                         step_name="AI OCR Correction",
                         progress_current=pages_done,
                         progress_total=actual_pages)

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


# ============================================================================
# REFINEMENT — TEXT AND IMAGE MODES
# ============================================================================

# Regex to parse gap tags
GAP_RE = re.compile(
    r'\{\{\s*gap\s*\|'
    r'\s*est=(\d+)'
    r'\s*\|\s*imgbbox="([^"]*)"'
    r'(?:\s*\|\s*cnf="([^"]*)")?'
    r'(?:\s*\|\s*status=(\S+))?'
    r'(?:\s*\|\s*fragments="([^"]*)")?'
    r'(?:\s*\|\s*region_ocr="([^"]*)")?'
    r'\s*\[([^\]]*)\]'
    r'\s*\}\}')


def parse_gaps(text: str) -> list:
    """Extract all gap tags from text with their positions and fields."""
    gaps = []
    for m in GAP_RE.finditer(text):
        gaps.append({
            "match": m,
            "start": m.start(),
            "end": m.end(),
            "est": int(m.group(1)),
            "imgbbox": m.group(2),
            "cnf": float(m.group(3)) if m.group(3) else 0.0,
            "status": m.group(4) or "",
            "fragments": m.group(5) or "",
            "region_ocr": m.group(6) or "",
            "guess": m.group(7),
            "full_tag": m.group(0),
        })
    return gaps


def get_context(text: str, start: int, end: int, chars: int = 200) -> str:
    """Get ~chars characters of context before and after a position."""
    before = text[max(0, start - chars):start].strip()
    after = text[end:end + chars].strip()
    return f"...{before} [GAP] {after}..."


def parse_bbox(bbox_str: str) -> tuple:
    """Parse 'x,y,w,h' string into (x, y, w, h) ints."""
    parts = bbox_str.split(",")
    if len(parts) == 4:
        return tuple(int(p.strip()) for p in parts)
    return (0, 0, 0, 0)


def merge_bboxes(bboxes: list, padding: int = 50) -> tuple:
    """Merge a list of (x,y,w,h) bboxes into a single bounding box."""
    if not bboxes:
        return (0, 0, 0, 0)
    x_min = min(b[0] for b in bboxes) - padding
    y_min = min(b[1] for b in bboxes) - padding
    x_max = max(b[0] + b[2] for b in bboxes) + padding
    y_max = max(b[1] + b[3] for b in bboxes) + padding
    return (max(0, x_min), max(0, y_min), x_max - max(0, x_min), y_max - max(0, y_min))


def group_gaps_by_bbox(gaps: list, proximity_px: int = 100) -> list:
    """Group gaps whose bboxes overlap or are within proximity_px vertically.
    Returns list of lists of gaps."""
    if not gaps:
        return []

    # Sort by y position
    sorted_gaps = sorted(gaps, key=lambda g: parse_bbox(g["imgbbox"])[1])
    groups = []
    current_group = [sorted_gaps[0]]
    current_bbox = parse_bbox(sorted_gaps[0]["imgbbox"])
    current_bottom = current_bbox[1] + current_bbox[3]

    for gap in sorted_gaps[1:]:
        bbox = parse_bbox(gap["imgbbox"])
        gap_top = bbox[1]
        if gap_top <= current_bottom + proximity_px:
            current_group.append(gap)
            current_bottom = max(current_bottom, bbox[1] + bbox[3])
        else:
            groups.append(current_group)
            current_group = [gap]
            current_bbox = bbox
            current_bottom = bbox[1] + bbox[3]

    groups.append(current_group)
    return groups


def crop_image(image_bytes: bytes, bbox: tuple) -> bytes:
    """Crop a JPEG image to the given (x,y,w,h) bounding box. Returns JPEG bytes.
    Requires PIL/Pillow: pip install pillow"""
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError(
            "PIL/Pillow required for image cropping: pip install pillow")
    import io
    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        raise RuntimeError(f"Failed to open image for cropping: {e}")
    x, y, w, h = bbox
    # Clamp to image bounds
    x2 = min(x + w, img.width)
    y2 = min(y + h, img.height)
    x = max(0, x)
    y = max(0, y)
    if x2 <= x or y2 <= y:
        raise RuntimeError(f"Invalid crop region: ({x},{y},{x2},{y2})")
    cropped = img.crop((x, y, x2, y2))
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def build_text_refine_prompt() -> str:
    """System prompt for text-only refinement."""
    fraktur_errors = load_reference("fraktur-errors.md")
    texas_german = load_reference("texas-german.md")
    return f"""You are an expert in 19th-century German Fraktur OCR correction.

REFERENCE: FRAKTUR ERROR PATTERNS
{fraktur_errors}

REFERENCE: TEXAS GERMAN VOCABULARY
{texas_german}

TASK: Re-evaluate OCR gap tags using fragments, raw OCR, and context.
No image is provided — work from text evidence only.

Note: the est (character count) in each gap is approximate. Fraktur is
proportional — narrow letters (l,i,t,f) are ~30-40% the width of wide
ones (W,M,m,w). A gap with est=12 could hold 8 wide or 18 narrow chars.
Prefer the reading that makes linguistic sense over matching est exactly.

For each gap in the batch, return one line:
  UNCHANGED: <N>
  UPDATED: <N> | cnf="0.XX" [new guess]
  PROMOTED: <N> [text to replace gap tag]

PROMOTED means cnf >= 0.95 — the gap tag is removed and replaced with
plain text. UPDATED means improved guess/confidence. UNCHANGED means
you cannot improve on the current guess.

Preserve Texas German dialect, pre-1901 spellings, English loanwords."""


def build_image_refine_prompt() -> str:
    """System prompt for image-assisted refinement."""
    fraktur_errors = load_reference("fraktur-errors.md")
    texas_german = load_reference("texas-german.md")
    return f"""You are an expert in reading 19th-century German Fraktur from
damaged microfilm scans.

REFERENCE: FRAKTUR ERROR PATTERNS
{fraktur_errors}

REFERENCE: TEXAS GERMAN VOCABULARY
{texas_german}

TASK: You receive a cropped region of a newspaper page and one or more
gap tags from that region with surrounding context. Examine the image
carefully and produce updated guesses.

Note: est (character count) is approximate. Fraktur is proportional —
narrow letters are ~30-40% the width of wide ones. Prefer linguistic
sense over matching the exact character count.

For each gap, return one line:
  UNCHANGED: <N>
  UPDATED: <N> | cnf="0.XX" [new guess]
  PROMOTED: <N> [text to replace gap tag]

PROMOTED means cnf >= 0.95. Look for Fraktur letterforms, ascenders,
descenders, dots, stroke patterns. Cross-reference with fragments and
raw OCR text provided.

Preserve Texas German dialect, pre-1901 spellings, English loanwords."""


def apply_refinement_results(page_text: str, gaps: list,
                             results: str) -> str:
    """Apply refinement results to page text, modifying gap tags in-place."""
    replacements = {}
    for line in results.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("UNCHANGED:"):
            continue
        elif line.startswith("UPDATED:"):
            m = re.match(
                r'UPDATED:\s*(\d+)\s*\|\s*cnf="([^"]*)"\s*\[([^\]]*)\]',
                line)
            if m:
                idx = int(m.group(1)) - 1
                new_cnf = m.group(2)
                new_guess = m.group(3)
                if 0 <= idx < len(gaps):
                    replacements[idx] = ("update", new_cnf, new_guess)
        elif line.startswith("PROMOTED:"):
            m = re.match(r'PROMOTED:\s*(\d+)\s*\[([^\]]*)\]', line)
            if m:
                idx = int(m.group(1)) - 1
                promoted_text = m.group(2)
                if 0 <= idx < len(gaps):
                    replacements[idx] = ("promote", None, promoted_text)

    # Apply replacements in reverse order to preserve positions
    sorted_indices = sorted(replacements.keys(), reverse=True)
    for idx in sorted_indices:
        action, new_cnf, new_text = replacements[idx]
        gap = gaps[idx]
        if action == "promote":
            page_text = (page_text[:gap["start"]] + new_text +
                         page_text[gap["end"]:])
        elif action == "update":
            cnf_val = float(new_cnf)
            status = ' | status=auto-resolved' if cnf_val >= 0.80 else ''
            frags = f' | fragments="{gap["fragments"]}"' if gap["fragments"] else ''
            rocr = f' | region_ocr="{gap["region_ocr"]}"' if gap["region_ocr"] else ''
            new_tag = (f'{{{{ gap | est={gap["est"]} | '
                       f'imgbbox="{gap["imgbbox"]}" | '
                       f'cnf="{new_cnf}"{status}{frags}{rocr} '
                       f'[{new_text}] }}}}')
            page_text = (page_text[:gap["start"]] + new_tag +
                         page_text[gap["end"]:])

    return page_text


def _refine_text_page(page_file, cnf_min, cnf_max, include_resolved,
                      model, prompt, api_key, rate_limiter, cost_tracker):
    """Refine one page's gaps (text-only). Returns (updated, promoted, unchanged)."""
    if _interrupted or (cost_tracker and cost_tracker.would_exceed_budget()):
        return 0, 0, 0
    text = page_file.read_text(encoding="utf-8")
    gaps = parse_gaps(text)

    eligible = [g for g in gaps
                if cnf_min <= g["cnf"] <= cnf_max
                and (include_resolved or g["status"] != "auto-resolved")
                and (g["fragments"] or g["region_ocr"])]

    if not eligible:
        return 0, 0, 0

    log_event(f"{page_file.parent.name}/{page_file.name}: "
              f"{len(eligible)} gaps")

    batch_lines = []
    for i, g in enumerate(eligible, 1):
        ctx = get_context(text, g["start"], g["end"])
        batch_lines.append(
            f"GAP {i}: est={g['est']} cnf={g['cnf']:.2f} "
            f"fragments=\"{g['fragments']}\" "
            f"region_ocr=\"{g['region_ocr']}\" "
            f"current_guess=\"{g['guess']}\"\n"
            f"  Context: {ctx}")

    user_msg = "\n\n".join(batch_lines)

    try:
        result, _ = claude_api_call(
            {"model": model, "max_tokens": 4000,
             "system": prompt,
             "messages": [{"role": "user", "content": user_msg}]},
            api_key, rate_limiter,
            est_tokens=len(user_msg) // 4 + 2000,
            cost_tracker=cost_tracker)
    except Exception as e:
        tprint(f"    Error: {e}", level=1)
        return 0, 0, 0

    new_text = apply_refinement_results(text, eligible, result)
    updated = promoted = unchanged = 0
    if new_text != text:
        page_file.write_text(new_text, encoding="utf-8")
        for line in result.strip().split("\n"):
            if line.startswith("UPDATED:"):
                updated += 1
            elif line.startswith("PROMOTED:"):
                promoted += 1
            elif line.startswith("UNCHANGED:"):
                unchanged += 1
    return updated, promoted, unchanged


def refine_text(issues, config, collection_dir, api_key,
                cnf_min=0.0, cnf_max=0.79, include_resolved=False,
                rate_limiter=None, cost_tracker=None, model=None,
                api_workers=3):
    """Text-only refinement pass across all matching gaps (parallelized)."""
    model = model or MODEL_RESOLVE
    prompt = build_text_refine_prompt()

    # Collect all page files
    page_files = []
    for issue in issues:
        ai_dir = collection_dir / "ai_ocr" / issue["ark_id"]
        if ai_dir.exists():
            page_files.extend(sorted(ai_dir.glob("page_*.md")))

    if not page_files:
        print("No ai_ocr/ pages found.")
        return 0

    total_updated = total_promoted = total_unchanged = 0

    def _run_text(pf):
        return _refine_text_page(
            pf, cnf_min, cnf_max, include_resolved,
            model, prompt, api_key, rate_limiter, cost_tracker)

    results = run_parallel(_run_text, page_files,
                           max_workers=api_workers, label="pages")
    for pf, res in results:
        if isinstance(res, Exception):
            continue
        u, p, uc = res
        total_updated += u
        total_promoted += p
        total_unchanged += uc

    print(f"\nText refinement: {total_promoted} promoted, "
          f"{total_updated} updated, {total_unchanged} unchanged")
    return total_promoted + total_updated


def _refine_image_page(page_file, ark_id, cnf_min, cnf_max,
                       include_resolved, model, prompt,
                       api_key, rate_limiter, cost_tracker):
    """Refine one page's gaps with image cropping. Returns (updated, promoted).
    Groups within a page are processed sequentially (they modify same text)."""
    if _interrupted or (cost_tracker and cost_tracker.would_exceed_budget()):
        return 0, 0
    text = page_file.read_text(encoding="utf-8")
    gaps = parse_gaps(text)

    eligible = [g for g in gaps
                if cnf_min <= g["cnf"] <= cnf_max
                and (include_resolved or g["status"] != "auto-resolved")
                and not (not g["fragments"] and not g["region_ocr"]
                         and g["cnf"] == 0.0)]

    if not eligible:
        return 0, 0

    pg_match = re.search(r'page_(\d+)', page_file.stem)
    if not pg_match:
        return 0, 0
    pg_num = int(pg_match.group(1))
    img_bytes, _ = fetch_page_image(ark_id, pg_num)
    if not img_bytes:
        tprint(f"  {ark_id}/p{pg_num}: no image, skipping", level=1)
        return 0, 0

    groups = group_gaps_by_bbox(eligible)
    tprint(f"  {ark_id}/p{pg_num}: {len(eligible)} gaps in "
           f"{len(groups)} batch(es)", level=1)

    updated = promoted = 0
    any_changed = False

    for group in groups:
        bboxes = [parse_bbox(g["imgbbox"]) for g in group]
        merged = merge_bboxes(bboxes, padding=50)

        try:
            crop_bytes = crop_image(img_bytes, merged)
        except Exception as e:
            tprint(f"    Crop failed: {e}", level=2)
            continue

        content = [
            {"type": "image", "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.standard_b64encode(
                    crop_bytes).decode("ascii"),
            }},
        ]

        batch_lines = []
        for i, g in enumerate(group, 1):
            ctx = get_context(text, g["start"], g["end"])
            batch_lines.append(
                f"GAP {i}: est={g['est']} cnf={g['cnf']:.2f} "
                f"fragments=\"{g['fragments']}\" "
                f"region_ocr=\"{g['region_ocr']}\" "
                f"current_guess=\"{g['guess']}\"\n"
                f"  Context: {ctx}")

        content.append({"type": "text",
                        "text": "\n\n".join(batch_lines)})

        try:
            result, _ = claude_api_call(
                {"model": model, "max_tokens": 2000,
                 "system": prompt,
                 "messages": [{"role": "user", "content": content}]},
                api_key, rate_limiter, est_tokens=5000,
                cost_tracker=cost_tracker)
        except Exception as e:
            tprint(f"    Error: {e}", level=1)
            continue

        new_text = apply_refinement_results(text, group, result)
        if new_text != text:
            text = new_text
            any_changed = True
            for line in result.strip().split("\n"):
                if line.startswith("UPDATED:"):
                    updated += 1
                elif line.startswith("PROMOTED:"):
                    promoted += 1

    if any_changed:
        page_file.write_text(text, encoding="utf-8")
    return updated, promoted


def refine_image(issues, config, collection_dir, api_key,
                 cnf_min=0.01, cnf_max=0.60, include_resolved=False,
                 rate_limiter=None, cost_tracker=None, model=None,
                 api_workers=3):
    """Image-assisted refinement with bbox cropping and batching (parallelized)."""
    model = model or MODEL_REFINE
    prompt = build_image_refine_prompt()

    # Collect all (page_file, ark_id) pairs
    page_items = []
    for issue in issues:
        ark_id = issue["ark_id"]
        ai_dir = collection_dir / "ai_ocr" / ark_id
        if ai_dir.exists():
            for pf in sorted(ai_dir.glob("page_*.md")):
                page_items.append((pf, ark_id))

    if not page_items:
        print("No ai_ocr/ pages found.")
        return 0

    total_updated = total_promoted = 0

    def _run_img(item):
        pf, aid = item
        return _refine_image_page(
            pf, aid, cnf_min, cnf_max, include_resolved,
            model, prompt, api_key, rate_limiter, cost_tracker)

    results = run_parallel(_run_img, page_items,
                           max_workers=api_workers, label="pages")
    for item, res in results:
        if isinstance(res, Exception):
            continue
        u, p = res
        total_updated += u
        total_promoted += p

    print(f"\nImage refinement: {total_promoted} promoted, "
          f"{total_updated} updated")
    return total_promoted + total_updated


def regenerate_corrected(issues, config, collection_dir):
    """Regenerate corrected/ and readable/ from updated ai_ocr/ files."""
    corrected_dir = collection_dir / "corrected"
    corrected_dir.mkdir(exist_ok=True)

    for issue in issues:
        ark_id = issue["ark_id"]
        vol = str(issue.get("volume", "?")).zfill(2)
        num = str(issue.get("number", "?")).zfill(2)
        date = re.sub(r"[^\w\-]", "-", issue.get("date", "unknown"))
        fname = f"{ark_id}_vol{vol}_no{num}_{date}.txt"
        ai_dir = collection_dir / "ai_ocr" / ark_id

        if not ai_dir.exists():
            continue

        page_files = sorted(ai_dir.glob("page_*.md"))
        if not page_files:
            continue

        # Build header
        title_line = issue.get("full_title", config.get("title_name", ""))
        header = (
            f"=== {title_line} ===\n"
            f"ARK:    {ark_id}\n"
            f"URL:    https://texashistory.unt.edu/ark:/67531/{ark_id}/\n"
            f"Date:   {issue.get('date', 'unknown')}\n"
            f"Volume: {issue.get('volume', '?')}   "
            f"Number: {issue.get('number', '?')}\n"
            f"Title:  {title_line}\n"
            f"{'=' * 60}")

        total_pages = len(page_files)
        out_lines = [header, ""]
        for pf in page_files:
            pg_match = re.search(r'page_(\d+)', pf.stem)
            pg_num = int(pg_match.group(1)) if pg_match else 0
            raw = pf.read_text(encoding="utf-8")
            clean = extract_clean_text(raw)
            out_lines.append(f"--- Page {pg_num} of {total_pages} ---")
            out_lines.append(clean)
            out_lines.append("")

        corr_path = corrected_dir / fname
        corr_path.write_text("\n".join(out_lines), encoding="utf-8")

    # Also regenerate readable/ if it exists
    readable_dir = collection_dir / "readable"
    if readable_dir.exists():
        compile_all(issues, config, collection_dir, resume=False)
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

def _model_price(model):
    """Get (input_$/MTok, output_$/MTok) for a model from pricing.json."""
    pricing = load_pricing() if load_pricing else {}
    mp = pricing.get(model, {})
    return mp.get("input", 3.0), mp.get("output", 15.0)


def show_cost_estimate(step_name, units, unit_label,
                       est_input_tok, est_output_tok, model, budget):
    """Display pre-batch cost estimate. Returns (est_cost, per_unit_cost)."""
    in_price, out_price = _model_price(model)
    per_unit = (est_input_tok * in_price +
                est_output_tok * out_price) / 1_000_000
    est_cost = units * per_unit
    est_high = est_cost * 1.2

    print()
    print("=" * 60)
    print(f"  {step_name} -- COST ESTIMATE")
    print("=" * 60)
    print(f"  Model            : {model} "
          f"(${in_price:.2f}/${out_price:.2f} per MTok)")
    print(f"  {unit_label:<18} : {units:,}")
    print(f"  Est. cost/unit   : ${per_unit:.4f}")
    print(f"  Est. total cost  : ${est_cost:.2f} - ${est_high:.2f}")
    if budget is not None:
        print(f"  Budget limit     : ${budget:.2f}")
        if est_high > budget:
            affordable = int(budget / per_unit) if per_unit > 0 else 0
            print(f"  Budget covers    : ~{affordable} of {units} {unit_label}")
    print("=" * 60)
    print()
    return est_cost, per_unit


def show_correction_estimate(pages_to_process, budget):
    """Cost estimate for tiered --correct (Haiku + Sonnet)."""
    p12_in, p12_out = _model_price(MODEL_INITIAL)
    p3_in, p3_out = _model_price(MODEL_RESOLVE)

    cost_p12 = (EST_INPUT_TOKENS_PASS12 * p12_in +
                EST_OUTPUT_TOKENS_PASS12 * p12_out) / 1_000_000
    cost_p3 = (EST_INPUT_TOKENS_PASS3 * p3_in +
               EST_OUTPUT_TOKENS_PASS3 * p3_out) / 1_000_000
    per_page = cost_p12 + cost_p3
    est_cost = pages_to_process * per_page
    est_high = est_cost * 1.2

    print()
    print("=" * 60)
    print("  AI OCR CORRECTION -- COST ESTIMATE")
    print("=" * 60)
    print(f"  Pass 1-2 model   : {MODEL_INITIAL} "
          f"(${p12_in:.2f}/${p12_out:.2f} per MTok)")
    print(f"  Pass 3 model     : {MODEL_RESOLVE} "
          f"(${p3_in:.2f}/${p3_out:.2f} per MTok)")
    print(f"  Pages to process : {pages_to_process:,}")
    print(f"  Est. cost/page   : ${per_page:.3f} "
          f"(${cost_p12:.3f} image + ${cost_p3:.3f} text)")
    print(f"  Est. total cost  : ${est_cost:.0f} - ${est_high:.0f}")
    if budget is not None:
        print(f"  Budget limit     : ${budget:.2f}")
        if est_high > budget:
            affordable = int(budget / per_page) if per_page > 0 else 0
            print(f"  Budget covers    : ~{affordable} of "
                  f"{pages_to_process} pages")
    print("=" * 60)
    print()
    return est_cost, per_page


def show_revised_estimate(cost_tracker, units_remaining, unit_label="pages"):
    """Show revised cost estimate based on actual usage so far."""
    est_remaining = cost_tracker.estimate_remaining(units_remaining)
    avg = cost_tracker.avg_cost_per_page()

    print()
    print("-" * 60)
    print(f"  REVISED ESTIMATE (after {cost_tracker.pages_processed} "
          f"{unit_label})")
    print("-" * 60)
    print(f"  Actual cost/unit : ${avg:.4f}")
    print(f"  Spent so far     : ${cost_tracker.total_cost:.2f}")
    print(f"  Remaining units  : {units_remaining}")
    print(f"  Est. remaining   : ${est_remaining:.2f}")
    print(f"  Est. total       : "
          f"${cost_tracker.total_cost + est_remaining:.2f}")
    if cost_tracker.budget is not None:
        remaining_budget = cost_tracker.budget - cost_tracker.total_cost
        print(f"  Budget remaining : ${remaining_budget:.2f}")
        if est_remaining > remaining_budget:
            affordable = int(remaining_budget / max(avg, 0.001))
            print(f"  Budget covers    : ~{affordable} more {unit_label}")
    print("-" * 60)
    print()


def _show_model_menu(api_key: str) -> list:
    """Fetch and display available models. Returns menu list."""
    try:
        from unt_cost_estimate import build_model_menu
        menu = build_model_menu(api_key)
    except Exception:
        menu = []

    if not menu:
        # Fallback: show known models from pricing.json
        pricing = load_pricing() if load_pricing else {}
        menu = [{"id": mid, "input": p.get("input"),
                 "output": p.get("output"), "tier": p.get("tier", ""),
                 "note": p.get("note", ""), "priced": True}
                for mid, p in pricing.items()]

    if menu:
        print(f"\n  {'#':<3}  {'Model ID':<36}  {'In$/MTok':>8}  "
              f"{'Out$/MTok':>9}  Note")
        print(f"  {'─'*3}  {'─'*36}  {'─'*8}  {'─'*9}  {'─'*20}")
        for i, m in enumerate(menu, 1):
            in_str = f"${m['input']:.2f}" if m.get("input") else "?"
            out_str = f"${m['output']:.2f}" if m.get("output") else "?"
            note = (m.get("note") or "")[:20]
            print(f"  {i:<3}  {m['id']:<36}  {in_str:>8}  "
                  f"{out_str:>9}  {note}")
        print()
    return menu


def _pick_model(prompt_text: str, current: str, menu: list) -> str:
    """Let user pick a model from the menu or type one. Returns model ID."""
    raw = input(f"    {prompt_text} [{current}]: ").strip()
    if not raw:
        return current
    # If they typed a number, look up in menu
    try:
        idx = int(raw)
        if 1 <= idx <= len(menu):
            return menu[idx - 1]["id"]
    except ValueError:
        pass
    # Otherwise treat as a model ID string
    return raw


def prompt_model_selection(skip=False, api_key=""):
    """Let user change model assignments before a batch starts.
    Shows available models from the API. Returns True if changed."""
    global MODEL_INITIAL, MODEL_RESOLVE, MODEL_REFINE
    if skip:
        return False

    print(f"  Current model assignments:")
    print(f"    1. Pass 1-2 (image):     {MODEL_INITIAL}")
    print(f"    2. Pass 3 (text):        {MODEL_RESOLVE}")
    print(f"    3. Refinement:           {MODEL_REFINE}")
    print()
    raw = input("  Change models? Enter number to edit, "
                "or press Enter to continue: ").strip()
    if not raw:
        return False

    # Fetch model list once
    menu = _show_model_menu(api_key) if api_key else []
    if menu:
        print("  Enter a number from the list above, or type a model ID.")
        print()

    changed = False
    while raw:
        if raw == "1":
            new = _pick_model("Pass 1-2 model", MODEL_INITIAL, menu)
            if new != MODEL_INITIAL:
                MODEL_INITIAL = new
                changed = True
        elif raw == "2":
            new = _pick_model("Pass 3 model", MODEL_RESOLVE, menu)
            if new != MODEL_RESOLVE:
                MODEL_RESOLVE = new
                changed = True
        elif raw == "3":
            new = _pick_model("Refinement model", MODEL_REFINE, menu)
            if new != MODEL_REFINE:
                MODEL_REFINE = new
                changed = True
        raw = input("  Edit another (1/2/3) or Enter to continue: ").strip()

    if changed:
        print(f"\n  Updated models:")
        print(f"    Pass 1-2: {MODEL_INITIAL}")
        print(f"    Pass 3:   {MODEL_RESOLVE}")
        print(f"    Refine:   {MODEL_REFINE}")
        print()
    return changed


def confirm_or_abort(prompt_msg, skip=False):
    """Prompt user for y/N confirmation. Returns True to proceed."""
    if skip:
        return True
    confirm = input(f"{prompt_msg} [y/N]: ").strip().lower()
    return confirm in ("y", "yes")


def check_revised_and_budget(cost_tracker, total_units, unit_label,
                             skip_estimate=False):
    """After REVISE_AFTER_PAGES, show revised estimate. If revised estimate
    exceeds budget, set abort flag and return False. Returns True to continue."""
    if not cost_tracker.should_show_revised():
        return True

    units_remaining = total_units - cost_tracker.pages_processed
    if units_remaining <= 0:
        return True

    show_revised_estimate(cost_tracker, units_remaining, unit_label)

    if cost_tracker.budget is not None:
        est_rem = cost_tracker.estimate_remaining(units_remaining)
        bud_rem = cost_tracker.budget - cost_tracker.total_cost
        if est_rem > bud_rem:
            avg = cost_tracker.avg_cost_per_page()
            affordable = int(bud_rem / max(avg, 0.001))
            print(f"  WARNING: Revised estimate ${est_rem:.2f} exceeds "
                  f"remaining budget ${bud_rem:.2f}.")
            print(f"  ~{affordable} more {unit_label} affordable.")
            if not confirm_or_abort("Continue?", skip=skip_estimate):
                cost_tracker._budget_abort = True
                return False
    return True


def count_refinement_gaps(issues, collection_dir, cnf_min, cnf_max,
                          include_resolved, require_evidence=False):
    """Count eligible gaps across all issues for pre-batch estimation."""
    total_gaps = 0
    total_pages_with_gaps = 0
    for issue in issues:
        ai_dir = collection_dir / "ai_ocr" / issue["ark_id"]
        if not ai_dir.exists():
            continue
        for pf in ai_dir.glob("page_*.md"):
            text = pf.read_text(encoding="utf-8")
            gaps = parse_gaps(text)
            eligible = 0
            for g in gaps:
                if g["cnf"] < cnf_min or g["cnf"] > cnf_max:
                    continue
                if g["status"] == "auto-resolved" and not include_resolved:
                    continue
                if require_evidence and not g["fragments"] and not g["region_ocr"]:
                    continue
                eligible += 1
            if eligible:
                total_gaps += eligible
                total_pages_with_gaps += 1
    return total_gaps, total_pages_with_gaps


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
    p.add_argument("--refine-text",    action="store_true",
                   help="Text-only refinement of low-confidence gaps "
                        "(no image, uses Sonnet by default)")
    p.add_argument("--refine-image",   action="store_true",
                   help="Image-assisted refinement of gaps using bbox "
                        "cropping (uses Opus by default)")
    p.add_argument("--cnf-min",        type=float, default=None,
                   help="Minimum cnf for refinement (default: 0.0 text, "
                        "0.01 image)")
    p.add_argument("--cnf-max",        type=float, default=None,
                   help="Maximum cnf for refinement (default: 0.79 text, "
                        "0.60 image)")
    p.add_argument("--include-resolved", action="store_true",
                   help="Include auto-resolved gaps in refinement")
    p.add_argument("--budget",         type=float, default=None,
                   help="Max dollar amount to spend (stops before exceeding)")
    p.add_argument("--model-initial",  default=None,
                   help="Model for Pass 1-2 image transcription "
                        "(default: claude-haiku-4-5)")
    p.add_argument("--model-resolve",  default=None,
                   help="Model for Pass 3 cross-reference/guess "
                        "(default: claude-sonnet-4-6)")
    p.add_argument("--model-refine",   default=None,
                   help="Model for future refinement passes "
                        "(default: claude-opus-4-6)")
    p.add_argument("--logging",        type=int, default=1,
                   choices=[1, 2, 3, 4, 5],
                   help="Log verbosity: 1=progress 2=pages 3=api 4=detail "
                        "5=verbose")
    p.add_argument("--verbose",        action="store_true",
                   help="Shorthand for --logging 5")
    p.add_argument("--yes",            action="store_true",
                   help="Skip cost confirmation prompt")
    p.add_argument("--skip-estimate",  action="store_true",
                   help="Skip cost estimates and revised estimates "
                        "(budget checks still apply)")
    # Accept but ignore these (passed by orchestrator)
    p.add_argument("--issue-delay",    type=float, default=None)
    p.add_argument("--max-output-tokens", type=int, default=None)
    args = p.parse_args()

    config_path = Path(args.config_path)
    if not config_path.exists():
        sys.exit(f"Config not found: {config_path}")
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    collection_dir = config_path.parent
    init_paths(collection_dir)
    init_logging(collection_dir,
                 5 if args.verbose else args.logging)

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

    # Model tier resolution: CLI flags → config.json → defaults
    global MODEL_INITIAL, MODEL_RESOLVE, MODEL_REFINE
    if args.model_initial:
        MODEL_INITIAL = args.model_initial
    elif global_config.get("model_initial"):
        MODEL_INITIAL = global_config["model_initial"]
    if args.model_resolve:
        MODEL_RESOLVE = args.model_resolve
    elif config.get("claude_model"):
        MODEL_RESOLVE = config["claude_model"]
    elif global_config.get("claude_model"):
        MODEL_RESOLVE = global_config["claude_model"]
    if args.model_refine:
        MODEL_REFINE = args.model_refine
    elif global_config.get("model_refine"):
        MODEL_REFINE = global_config["model_refine"]

    # API key resolution
    api_key = (args.api_key
               or os.environ.get("ANTHROPIC_API_KEY", "")
               or global_config.get("anthropic_api_key", "")
               or config.get("anthropic_api_key", ""))
    if (not args.preload_images and not args.compile
            and not api_key):
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
    print(f"Models     : {MODEL_INITIAL} (pass 1-2) → "
          f"{MODEL_RESOLVE} (pass 3)", flush=True)
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

    if args.refine_text or args.refine_image:
        effective_workers = 1 if args.serial else args.api_workers
        rate_limiter = (limiter_from_tier(args.tier)
                        if ClaudeRateLimiter else None)

        # Let user change models before refinement
        if not args.skip_estimate:
            prompt_model_selection(skip=args.yes, api_key=api_key)

        if args.refine_text:
            cnf_min = args.cnf_min if args.cnf_min is not None else 0.0
            cnf_max = args.cnf_max if args.cnf_max is not None else 0.79
            model = args.model_resolve or MODEL_RESOLVE

            # Count eligible gaps and estimate cost
            total_gaps, pages_with_gaps = count_refinement_gaps(
                issues, collection_dir, cnf_min, cnf_max,
                args.include_resolved, require_evidence=True)
            print(f"\nText refinement: {total_gaps} gaps across "
                  f"{pages_with_gaps} pages (cnf {cnf_min:.2f}-{cnf_max:.2f},"
                  f" model: {model})")

            if total_gaps == 0:
                print("  No eligible gaps found.")
            else:
                # ~2k tokens per page batch (gaps + context)
                if not args.skip_estimate:
                    est, _ = show_cost_estimate(
                        "TEXT REFINEMENT", pages_with_gaps, "pages",
                        2000, 1000, model, args.budget)
                    if args.budget and est > args.budget:
                        if not confirm_or_abort(
                                "Estimate exceeds budget. Proceed?",
                                skip=args.yes):
                            print("Cancelled.")
                            return

                if confirm_or_abort(
                        f"Proceed with text refinement?",
                        skip=args.yes):
                    cost_tracker = CostTracker(model, budget=args.budget)
                    refine_text(issues, config, collection_dir, api_key,
                                cnf_min=cnf_min, cnf_max=cnf_max,
                                include_resolved=args.include_resolved,
                                rate_limiter=rate_limiter,
                                cost_tracker=cost_tracker,
                                model=model,
                                api_workers=effective_workers)
                    print(f"  {cost_tracker.summary()}")

        if args.refine_image:
            cnf_min = args.cnf_min if args.cnf_min is not None else 0.01
            cnf_max = args.cnf_max if args.cnf_max is not None else 0.60
            model = args.model_refine or MODEL_REFINE

            total_gaps, pages_with_gaps = count_refinement_gaps(
                issues, collection_dir, cnf_min, cnf_max,
                args.include_resolved, require_evidence=False)
            print(f"\nImage refinement: {total_gaps} gaps across "
                  f"{pages_with_gaps} pages (cnf {cnf_min:.2f}-{cnf_max:.2f},"
                  f" model: {model})")

            if total_gaps == 0:
                print("  No eligible gaps found.")
            else:
                # ~5k tokens per batch (cropped image + context)
                if not args.skip_estimate:
                    est, _ = show_cost_estimate(
                        "IMAGE REFINEMENT", total_gaps, "gap batches",
                        5000, 500, model, args.budget)
                    if args.budget and est > args.budget:
                        if not confirm_or_abort(
                                "Estimate exceeds budget. Proceed?",
                                skip=args.yes):
                            print("Cancelled.")
                            return

                if confirm_or_abort(
                        f"Proceed with image refinement?",
                        skip=args.yes):
                    cost_tracker = CostTracker(model, budget=args.budget)
                    refine_image(issues, config, collection_dir, api_key,
                                 cnf_min=cnf_min, cnf_max=cnf_max,
                                 include_resolved=args.include_resolved,
                                 rate_limiter=rate_limiter,
                                 cost_tracker=cost_tracker,
                                 model=model,
                                 api_workers=effective_workers)
                    print(f"  {cost_tracker.summary()}")

        # Regenerate corrected/ and readable/ from updated ai_ocr/
        print("\nRegenerating corrected/ and readable/ ...", flush=True)
        regenerate_corrected(issues, config, collection_dir)
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

    # Cost estimate and model selection
    if not args.skip_estimate:
        est_cost, per_page = show_correction_estimate(
            pages_to_process, args.budget)

        # Let user change models before committing
        if prompt_model_selection(skip=args.yes, api_key=api_key):
            # Re-estimate with new models
            est_cost, per_page = show_correction_estimate(
                pages_to_process, args.budget)

        # Abort if initial estimate exceeds budget
        if args.budget is not None and est_cost > args.budget:
            print(f"  Initial estimate ${est_cost:.2f} exceeds budget "
                  f"${args.budget:.2f}.")
            if not confirm_or_abort("Proceed anyway?", skip=args.yes):
                print("Cancelled.")
                return

    if not confirm_or_abort(
            f"Proceed with AI OCR correction? ({pages_to_process} pages)",
            skip=args.yes):
        print("Cancelled.")
        return

    # Initialize tracking
    pass12_prompt = build_pass12_prompt(config)
    pass3_prompt = build_pass3_prompt(config)
    rate_limiter = (limiter_from_tier(args.tier)
                    if ClaudeRateLimiter else None)
    if args.tier == "default":
        print("  Note: using 'default' rate tier (40k TPM). If you have"
              " Build tier access,\n"
              "  use --tier build for faster processing (80k TPM).",
              flush=True)
    cost_tracker = CostTracker(MODEL_RESOLVE, budget=args.budget)

    log = []
    log_lock = threading.Lock()
    ctr = {"ok": 0, "skipped": 0, "err": 0}
    effective_workers = 1 if args.serial else args.api_workers

    for idx, issue in enumerate(issues):
        ark_id = issue["ark_id"]
        tprint(f"\n{'=' * 60}", level=1)
        tprint(f"[{idx + 1}/{len(issues)}] {ark_id}  "
               f"Vol.{issue.get('volume', '?')} "
               f"No.{issue.get('number', '?')}  "
               f"{issue.get('date', '')}", level=1)

        # Budget or interrupt check before starting issue
        if _interrupted:
            print("\n  Interrupted by user.", flush=True)
            break
        if cost_tracker.would_exceed_budget():
            tprint(f"\nBUDGET LIMIT REACHED. {cost_tracker.summary()}",
                   level=1)
            break

        status = process_issue(
            issue, api_key, pass12_prompt, pass3_prompt,
            args.delay, args.resume, args.force,
            rate_limiter=rate_limiter,
            cost_tracker=cost_tracker,
            worker_id="",
            api_workers=effective_workers)

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

        # Issue-level summary
        done_issues = ctr["ok"] + ctr["skipped"] + ctr["err"]
        if len(issues) > 1:
            print(f"\n  Issue {done_issues}/{len(issues)} complete",
                  flush=True)

        # Revised estimate after 5 pages, budget abort if exceeded
        if not args.skip_estimate:
            if not check_revised_and_budget(
                    cost_tracker, pages_to_process, "pages",
                    skip_estimate=args.yes):
                print("Stopped: revised estimate exceeds budget.")
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
