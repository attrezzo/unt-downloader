#!/usr/bin/env python3
"""
UNT Archive — Multi-Engine OCR Correction Pipeline
====================================================

PIPELINE OVERVIEW
-----------------
For every page of every issue, the following stages run in order:

  Stage 1  ABBYY XML (optional)
           If abbyy/{issue}.xml exists, parse it for word tokens with
           bounding boxes, confidence flags, and block boundaries.
           ABBYY was run ~20+ years ago on older software; we treat it
           as one voice among several, not ground truth.

  Stage 2  PREPROCESSING (OpenCV, always runs)
           CLAHE contrast enhancement + median despeckling on the page scan.
           Prepares the image for both column detection and OCR.

  Stage 3  COLUMN DETECTION (OpenCV, always runs)
           Vertical dark-pixel projection on the bottom 30% of the content
           area (below mastheads/announcements). Finds N-1 gutter valleys
           by prominence. Always runs from the image regardless of ABBYY.

  Stage 4  BOUNDARY COMPARISON (when ABBYY present)
           Compare OpenCV-detected column gutters against ABBYY block
           boundaries (Separator blocks and block left-edge clusters).
           Discrepancies > 20px are logged and the wider agreement wins.
           This catches layout drift between the 20-year-old ABBYY run
           and the current image.

  Stage 5  TESSERACT (two passes per column strip, free)
           PSM-6 and PSM-4 runs produce independent word token lists
           with per-word confidence and bounding boxes.

  Stage 6  KRAKEN (third pass per column strip, if installed)
           Adds a third independent reading. Install: pip install kraken

  Stage 7  WORD ALIGNMENT + AGREEMENT ANALYSIS
           Align all available sources (ABBYY + Tesseract x2 + Kraken)
           by bounding-box proximity. Classify each word position as:
             AGREE    — all sources match AND min confidence ≥ 40
             DISPUTE  — sources disagree or any source below threshold
           Low-confidence ABBYY words (suspect=true) treated as disputes.

  Stage 8  CLAUDE ARBITRATION (API cost, targeted)
           Claude receives the page image + pre-built context:
             • Agreed text (clean, column-ordered) — no attention needed
             • Dispute table: each disputed position with every source's
               reading and confidence score
           Claude resolves disputes using image evidence and Fraktur
           pattern knowledge. Agreed words never reach Claude.

  Stage 9  ARTICLE SEGMENTATION + STITCHING
           Claude segments the corrected page into discrete articles/ads.
           Cross-page articles are detected and merged across page boundaries.

UNINTELLIGIBLE MARKER
  [unleserlich] — used everywhere, exactly as written.
  Sources: ABBYY suspect words, Tesseract conf=0 or conf<10, Kraken
           low-confidence, Claude (unresolvable even with image).

ABBYY SETUP
  mkdir {collection}/abbyy/       ← created automatically with README
  Files named like ocr/ but .xml: metapth1478562_vol01_no01_1891-09-17.xml
  Request from: ana.krahmer@unt.edu (UNT Digital Projects Unit)

TESSERACT SETUP
  apt-get install tesseract-ocr tesseract-ocr-deu
  # Better Fraktur model:
  # wget https://github.com/tesseract-ocr/tessdata_best/raw/main/deu.traineddata
  # cp deu.traineddata /usr/share/tesseract-ocr/5/tessdata/

KRAKEN SETUP (optional)
  pip install kraken
  kraken models download 10.0.0

ARTICLE FILE FORMAT
  articles/{ark_id}/pg{NN}_art{NNN}.txt  — one continuous article or ad
  articles/{ark_id}/manifest.json        — issue index

USAGE
  python unt_archive_downloader.py --correct --resume
  python unt_ocr_correct.py --config-path collection.json [--resume] [--ark ID]
  python unt_ocr_correct.py --config-path collection.json --preload-images
"""

import os, sys, json, time, re, base64, argparse, threading, shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request, urllib.error

# ── Optional image/OCR dependencies ─────────────────────────────────────────
try:
    import cv2
    import numpy as np
    from scipy.ndimage import uniform_filter1d
    from scipy.signal import find_peaks
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    import pytesseract
    from PIL import Image as PILImage
    # On Windows, Tesseract is often installed outside PATH
    if sys.platform == "win32" and not shutil.which("tesseract"):
        _win_tess_paths = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"),
        ]
        for _p in _win_tess_paths:
            if os.path.isfile(_p):
                pytesseract.pytesseract.tesseract_cmd = _p
                break
    HAS_TESSERACT = bool(
        shutil.which("tesseract")
        or (sys.platform == "win32"
            and os.path.isfile(getattr(pytesseract.pytesseract, 'tesseract_cmd', '')))
    )
except ImportError:
    HAS_TESSERACT = False

try:
    from kraken import blla, rpred
    from kraken.lib import models as kraken_models
    HAS_KRAKEN = True
    _KRAKEN_MODEL = None   # lazy-loaded on first use
except ImportError:
    HAS_KRAKEN = False

try:
    # iopath (used by LayoutParser/Detectron2) has a Windows bug where
    # cached URLs contain '?' which is invalid in Windows filenames.
    # Fix: monkey-patch iopath's _get_local_path to sanitize filenames.
    if sys.platform == "win32":
        try:
            import iopath.common.file_io as _iopath_fio
            _orig_http_get_local = _iopath_fio.HTTPURLHandler._get_local_path
            def _patched_http_get_local(self, path, **kwargs):
                # Sanitize the cached filename by replacing invalid chars
                import urllib.parse
                parsed = urllib.parse.urlparse(path)
                clean = parsed._replace(query="", fragment="").geturl()
                return _orig_http_get_local(self, clean, **kwargs)
            _iopath_fio.HTTPURLHandler._get_local_path = _patched_http_get_local
        except Exception:
            pass
    import layoutparser as lp
    HAS_LAYOUTPARSER = True
    _LP_MODEL = None   # lazy-loaded on first use
except ImportError:
    HAS_LAYOUTPARSER = False

try:
    from claude_rate_limiter import ClaudeRateLimiter, limiter_from_tier
except ImportError:
    ClaudeRateLimiter = None

try:
    from unt_cost_estimate import choose_model_and_confirm
except ImportError:
    choose_model_and_confirm = None

# ── Constants ────────────────────────────────────────────────────────────────
UNT_BASE      = "https://texashistory.unt.edu"
ANTHROPIC_API = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL  = "claude-sonnet-4-6"

TESS_LANG_PRIORITY = ["deu_frak+deu", "deu_frak", "deu", "eng"]
TESS_PSM_A = "--psm 6 --oem 1 --dpi 300"   # uniform text block
TESS_PSM_B = "--psm 4 --oem 1 --dpi 300"   # single column of text
TESS_CONF_MIN = 40    # below this = disputed regardless of agreement

# Buffer added to each side of cropped regions (columns, ads, etc.)
# before OCR.  ~2-3 characters of overlap catches text that straddles
# zone boundaries.  The alignment/arbitration steps filter orphans.
CROP_BUFFER_PX = 20

# The single canonical unintelligible marker — used by every source
ILLEGIBLE = "[unleserlich]"

sys.stdout.reconfigure(line_buffering=True)
_print_lock = threading.Lock()
def _sanitize_date(date_str: str) -> str:
    """Replace non-word/non-hyphen chars in a date string for use in filenames."""
    return re.sub(r'[^\w-]', '-', date_str)

# ── Logging levels ──────────────────────────────────────────────────────────
# 1 = issue progress + final results (default)
# 2 = page-level progress (local OCR done, Claude done, proofread)
# 3 = engine handoffs (Tesseract cmd, Kraken model, Claude API, segmentation)
# 4 = alignment detail (per-column disputes, word counts, boundary reports)
# 5 = verbose (dispute table samples, prompt snippets, token usage, timing)
LOG_LEVEL = 1

def tprint(*args, worker: str = "", level: int = 1, **kwargs):
    """Print with optional log level gating. level=0 always prints."""
    if level > LOG_LEVEL:
        return
    with _print_lock:
        prefix = f"[{worker}] " if worker else ""
        print(prefix, *args, flush=True, **kwargs)

# ── Global paths ─────────────────────────────────────────────────────────────
METADATA_DIR = OCR_DIR = CORRECTED_DIR = IMAGES_DIR = None
ABBYY_DIR    = ARTICLES_DIR = None

def init_paths(collection_dir: Path):
    global METADATA_DIR, OCR_DIR, CORRECTED_DIR, IMAGES_DIR, ABBYY_DIR, ARTICLES_DIR
    METADATA_DIR  = collection_dir / "metadata"
    OCR_DIR       = collection_dir / "ocr"
    CORRECTED_DIR = collection_dir / "corrected"
    IMAGES_DIR    = collection_dir / "images"
    ABBYY_DIR     = collection_dir / "abbyy"
    ARTICLES_DIR  = collection_dir / "articles"
    for d in [CORRECTED_DIR, ABBYY_DIR, ARTICLES_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    readme = ABBYY_DIR / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Place ABBYY XML files here, named to match ocr/ files but .xml extension.\n"
            "Example: metapth1478562_vol01_no01_1891-09-17.xml\n\n"
            "Request from UNT Digital Projects Unit: ana.krahmer@unt.edu\n"
            "These files contain word-level bounding boxes from ABBYY FineReader.\n"
            "They are used as one OCR source among several — not treated as ground truth,\n"
            "since they were generated 20+ years ago on older OCR software.\n"
            "ABBYY XML is optional — the pipeline runs without it.\n",
            encoding="utf-8")


# ============================================================================
# STAGE 1 — ABBYY XML PARSING
# ============================================================================
# A WordToken is a dict: {text, conf, source, left, top, right, bottom}
# A BlockBoundary is:    {type, left, top, right, bottom}
# 'text' is the OCR reading; ILLEGIBLE for suspect/unrecognized words.

def _strip_ns(tag: str) -> str:
    """Strip XML namespace from a tag name."""
    return re.sub(r'\{[^}]*\}', '', tag)

def parse_abbyy_page(xml_path: Path, page_index: int = 0) -> tuple:
    """
    Parse one page from an ABBYY FineReader XML file.

    Returns:
      tokens  — list of WordToken dicts (reading order: top→bottom, left→right)
      blocks  — list of BlockBoundary dicts (for column boundary cross-check)

    ABBYY XML structure (FineReader 10/11/15 schema):
      <document>
        <page width="..." height="...">
          <block blockType="Text|Picture|Table|Separator" l="..." t="..." r="..." b="...">
            <text>
              <par>
                <line baseline="...">
                  <formatting>
                    <charParams l t r b suspicious="true|false" wordStart="true|false">
                      char
                    </charParams>

    suspicious="true" means ABBYY was not confident about that character.
    We mark entire words as ILLEGIBLE if any char is suspicious.
    wordStart="true" marks the beginning of a new word.
    """
    import xml.etree.ElementTree as ET

    word_tokens  = []
    block_bounds = []

    try:
        tree  = ET.parse(xml_path)
        root  = tree.getroot()

        def findall_local(parent, local_tag):
            return [c for c in parent.iter() if _strip_ns(c.tag) == local_tag]

        pages = findall_local(root, "page")
        if page_index >= len(pages):
            return [], []
        page = pages[page_index]

        for block in findall_local(page, "block"):
            btype = block.get("blockType", "Text")
            try:
                bl = int(block.get("l", 0)); bt = int(block.get("t", 0))
                br = int(block.get("r", 0)); bb = int(block.get("b", 0))
            except (ValueError, TypeError):
                bl = bt = br = bb = 0
            block_bounds.append({"type": btype,
                                  "left": bl, "top": bt, "right": br, "bottom": bb})

            if btype != "Text":
                continue

            # Accumulate chars → words
            cur_chars   = []
            cur_suspect = False
            cur_l = cur_t = cur_r = cur_b = 0

            def flush_word():
                nonlocal cur_chars, cur_suspect, cur_l, cur_t, cur_r, cur_b
                if not cur_chars:
                    return
                raw = "".join(cur_chars).strip()
                if not raw:
                    cur_chars = []; cur_suspect = False
                    return
                word_tokens.append({
                    "text":   ILLEGIBLE if cur_suspect else raw,
                    "conf":   0 if cur_suspect else 80,
                    "source": "abbyy",
                    "left": cur_l, "top": cur_t, "right": cur_r, "bottom": cur_b,
                })
                cur_chars = []; cur_suspect = False

            for cp in findall_local(block, "charParams"):
                ch      = (cp.text or "").strip()
                suspect = cp.get("suspicious", "false").lower() == "true"
                wstart  = cp.get("wordStart",  "false").lower() == "true"
                try:
                    cl = int(cp.get("l", 0)); ct = int(cp.get("t", 0))
                    cr = int(cp.get("r", 0)); cb = int(cp.get("b", 0))
                except (ValueError, TypeError):
                    cl = ct = cr = cb = 0

                if wstart and cur_chars:
                    flush_word()

                if not ch:
                    if cur_chars:
                        flush_word()
                    continue

                if not cur_chars:
                    cur_l, cur_t = cl, ct
                cur_r, cur_b = cr, cb
                cur_chars.append(ch)
                if suspect:
                    cur_suspect = True

            flush_word()

        # Sort tokens into reading order
        word_tokens.sort(key=lambda t: (t["top"] // 12, t["left"]))
        return word_tokens, block_bounds

    except Exception as e:
        tprint(f"  ⚠ ABBYY parse error ({xml_path.name} page {page_index}): {e}", level=2)
        return [], []


def abbyy_xml_path(issue_fname: str) -> Path | None:
    """Return ABBYY XML path for this issue if it exists and is non-empty."""
    p = ABBYY_DIR / (Path(issue_fname).stem + ".xml")
    return p if p.exists() and p.stat().st_size > 200 else None


def abbyy_column_boundaries(block_bounds: list) -> list:
    """
    Derive column gutter x-positions from ABBYY block boundaries.

    Strategy:
      1. Collect left edges of all Text blocks (each column's leftmost block
         starts at the column's left edge after the gutter).
      2. Cluster them — close left edges belong to the same column.
      3. Separator blocks (if present) give direct gutter positions.

    Returns sorted list of x-positions that likely correspond to column gutters.
    """
    if not block_bounds:
        return []

    # Separator blocks give direct gutter evidence
    separator_xs = [b["left"] for b in block_bounds if b["type"] == "Separator"]

    # Text block left edges cluster at column left boundaries
    text_lefts = sorted(b["left"] for b in block_bounds if b["type"] == "Text")

    # Cluster text_lefts: positions within 25px of each other = same column
    clusters = []
    for x in text_lefts:
        if not clusters or x - clusters[-1][-1] > 25:
            clusters.append([x])
        else:
            clusters[-1].append(x)
    cluster_centers = [int(sum(c) / len(c)) for c in clusters]

    # Gutters fall between column left-edges
    # Approximate gutter as midpoint between adjacent column starts
    candidate_gutters = []
    for i in range(len(cluster_centers) - 1):
        candidate_gutters.append((cluster_centers[i] + cluster_centers[i+1]) // 2)

    # Merge with separator positions
    all_gutters = sorted(set(candidate_gutters + separator_xs))
    return all_gutters


# ============================================================================
# STAGE 0 — LAYOUT ANALYSIS (runs once per issue, before OCR)
# ============================================================================
# Analyzes all page images to determine the consistent column structure,
# detect mastheads, and produce debug overlay images with colored bboxes.
# Outputs:
#   - expected_cols: int (mode of column counts across all pages)
#   - page_layouts: dict of page_num → PageLayout
#   - overlay images saved to artifacts/layout/ (if artifacts dir exists)

# Zone types for colored overlay rendering
ZONE_COLORS = {
    "masthead":  (255, 100, 100),   # red
    "column":    (100, 200, 100),   # green
    "ad":        (100, 100, 255),   # blue
    "footer":    (200, 100, 200),   # purple
    "figure":    (255, 200, 0),     # orange
    "separator": (200, 200, 0),     # yellow
    "unknown":   (180, 180, 180),   # gray
}


def _detect_rule_lines(img_gray, content_bounds) -> dict:
    """
    Detect continuous black rule lines that form column borders and section
    separators. Most newspapers use thin (~1-3px) black lines running the
    full height (vertical) or width (horizontal) of the content area.

    Returns:
      {"vertical": [x1, x2, ...],    # x-coords of vertical rule lines
       "horizontal": [y1, y2, ...]}   # y-coords of horizontal rule lines
    """
    if not HAS_CV2:
        return {"vertical": [], "horizontal": []}
    left, top, right, bot = content_bounds
    cw = right - left
    ch = bot - top
    if cw < 100 or ch < 100:
        return {"vertical": [], "horizontal": []}

    # Binarize: dark pixels = 1, light = 0
    _, bw = cv2.threshold(img_gray, 80, 1, cv2.THRESH_BINARY_INV)
    content = bw[top:bot, left:right]

    # ── Vertical rule lines ──────────────────────────────────────────────
    # A real column divider is dark for a large fraction of the page height.
    # Normal text columns are dark only ~30-50% (letters + whitespace).
    # Rule lines are dark >60% of the height in a narrow band.
    col_dark_frac = content.mean(axis=0)  # fraction of dark pixels per x

    # Find narrow peaks (1-5px wide) where darkness is high
    # Smooth very lightly to merge adjacent pixel lines
    col_smooth = uniform_filter1d(col_dark_frac, size=3)

    # A rule line stands out as a spike above its neighbors.
    # Use local contrast: compare each position to a wide local average
    col_local_avg = uniform_filter1d(col_dark_frac, size=40)
    col_contrast = col_smooth - col_local_avg

    # Rule lines: darkness > 55% AND stands out > 15% above local average
    rule_threshold = 0.55
    contrast_threshold = 0.12
    v_candidates = []
    i = 0
    while i < len(col_dark_frac):
        if col_smooth[i] > rule_threshold and col_contrast[i] > contrast_threshold:
            # Found start of a rule line — find its extent
            j = i
            while j < len(col_dark_frac) and col_smooth[j] > rule_threshold * 0.7:
                j += 1
            # Rule line center (must be narrow: 1-8px)
            width = j - i
            if width <= 8:
                center = left + (i + j) // 2
                v_candidates.append(center)
            i = j + 1
        else:
            i += 1

    # Merge rule lines that are very close (within 5px) — probably the same line
    vertical = []
    for vx in v_candidates:
        if not vertical or vx - vertical[-1] > 5:
            vertical.append(vx)
        else:
            # Average with the previous detection
            vertical[-1] = (vertical[-1] + vx) // 2

    tprint(f"      rule lines: {len(vertical)} vertical at x={vertical}", level=4)

    # ── Horizontal rule lines ────────────────────────────────────────────
    # Same approach but row-wise. Horizontal rules span most of the width.
    row_dark_frac = content.mean(axis=1)
    row_smooth = uniform_filter1d(row_dark_frac, size=3)
    row_local_avg = uniform_filter1d(row_dark_frac, size=40)
    row_contrast = row_smooth - row_local_avg

    h_candidates = []
    i = 0
    while i < len(row_dark_frac):
        if row_smooth[i] > rule_threshold and row_contrast[i] > contrast_threshold:
            j = i
            while j < len(row_dark_frac) and row_smooth[j] > rule_threshold * 0.7:
                j += 1
            width = j - i
            if width <= 8:
                center = top + (i + j) // 2
                h_candidates.append(center)
            i = j + 1
        else:
            i += 1

    horizontal = []
    for hy in h_candidates:
        if not horizontal or hy - horizontal[-1] > 5:
            horizontal.append(hy)
        else:
            horizontal[-1] = (horizontal[-1] + hy) // 2

    tprint(f"      rule lines: {len(horizontal)} horizontal at y={horizontal}", level=4)

    return {"vertical": vertical, "horizontal": horizontal}


def _gutter_profile_band(img_gray, left, right, y_start, y_end):
    """
    Build a gutter-pattern score for a single narrow horizontal band.
    Same algorithm as _gutter_profile but for one band only.

    Returns gutter_score in LOCAL coords (0 = left edge of content).
    """
    region = img_gray[y_start:y_end, left:right]
    composite = region.mean(axis=0).astype(float)
    n = len(composite)
    local_bg = uniform_filter1d(composite, size=30)
    darkness_dip = uniform_filter1d(local_bg - composite, size=3)
    flank_score = np.zeros(n)
    for x in range(5, n - 5):
        fl = composite[x - 5 : x - 1].mean()
        fr = composite[x + 2 : x + 6].mean()
        flank_score[x] = min(fl, fr) - composite[x]
    d_max, f_max = darkness_dip.max(), flank_score.max()
    d_norm = np.clip(darkness_dip / d_max, 0, 1) if d_max > 0 else np.zeros(n)
    f_norm = np.clip(flank_score / f_max, 0, 1) if f_max > 0 else np.zeros(n)
    return np.sqrt(d_norm * f_norm)


def _estimate_skew(img_gray, content_bounds, n_cols=6):
    """
    Estimate page skew by fitting straight lines through gutter-pattern
    peaks at multiple heights.

    Rule lines in printed newspapers are always perfectly straight.  On a
    skewed scan they remain straight but tilted.  By measuring the gutter
    pattern at 10 narrow bands and fitting a robust line (Theil-Sen) through
    each gutter's positions, we recover the tilt angle.

    Returns skew in degrees (positive = clockwise tilt).
    Returns 0.0 if HAS_CV2 is False or the signal is too weak.
    """
    if not HAS_CV2:
        return 0.0
    import math as _math
    left, top, right, bot = content_bounds
    cw, ch = right - left, bot - top
    if cw < 200 or ch < 200:
        return 0.0

    n_bands = 10
    spacing = cw / n_cols
    window = max(8, int(spacing * 0.15))

    # Collect gutter peak positions at each band
    band_ys = []
    band_gutters = []   # list of lists: band_gutters[band][gutter_idx]
    for bi in range(n_bands):
        frac = 0.15 + 0.70 * bi / (n_bands - 1)
        yt = top + int(ch * (frac - 0.04))
        yb = top + int(ch * (frac + 0.04))
        y_center = (yt + yb) // 2
        band_ys.append(y_center)

        gs = _gutter_profile_band(img_gray, left, right, yt, yb)
        gutters = []
        for k in range(1, n_cols):
            gx = int(spacing * k)
            lo = max(0, gx - window)
            hi = min(len(gs), gx + window + 1)
            snap_x = lo + int(np.argmax(gs[lo:hi]))
            gutters.append(snap_x)
        band_gutters.append(gutters)

    # Fit a straight line per gutter using Theil-Sen (robust to outliers)
    ys = np.array(band_ys, dtype=float)
    slopes = []
    for gi in range(n_cols - 1):
        xs = np.array([bg[gi] for bg in band_gutters], dtype=float)
        pairwise = []
        for i in range(len(ys)):
            for j in range(i + 1, len(ys)):
                if ys[j] != ys[i]:
                    pairwise.append((xs[j] - xs[i]) / (ys[j] - ys[i]))
        if pairwise:
            slopes.append(float(np.median(pairwise)))

    if not slopes:
        return 0.0

    median_slope = float(np.median(slopes))
    skew_deg = _math.degrees(_math.atan(median_slope))
    return skew_deg


def _deskew_image(img_gray, skew_deg):
    """
    Rotate the image to correct the given skew angle.
    Returns the rotated image (same dimensions, black fill at borders).
    """
    if not HAS_CV2 or abs(skew_deg) < 0.05:
        return img_gray
    h, w = img_gray.shape
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, skew_deg, 1.0)
    return cv2.warpAffine(img_gray, M, (w, h),
                          flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def _refine_gutters_multiband(img_gray, content_bounds, n_cols,
                              initial_gutters):
    """
    Refine gutter x-positions using the straight-line constraint.

    Rule lines are straight, so the gutter x-position at any y should lie
    on a line x = slope*y + intercept.  We fit this line from 10 narrow
    bands (Theil-Sen estimator, robust to ads/headlines that shift peaks)
    and return the x-position at the page's vertical center.

    Returns refined list of gutter x-coordinates.
    """
    if not HAS_CV2:
        return initial_gutters
    left, top, right, bot = content_bounds
    cw, ch = right - left, bot - top
    spacing = cw / n_cols
    window = max(8, int(spacing * 0.15))

    n_bands = 10
    band_ys = []
    band_peaks = []   # [band][gutter_idx] = x in page coords
    for bi in range(n_bands):
        frac = 0.15 + 0.70 * bi / (n_bands - 1)
        yt = top + int(ch * (frac - 0.04))
        yb = top + int(ch * (frac + 0.04))
        band_ys.append((yt + yb) // 2)

        gs = _gutter_profile_band(img_gray, left, right, yt, yb)
        peaks = []
        for gi, init_x in enumerate(initial_gutters):
            # Search near the initial position, not equidistant
            local_x = init_x - left
            lo = max(0, local_x - window)
            hi = min(len(gs), local_x + window + 1)
            snap_x = lo + int(np.argmax(gs[lo:hi])) + left
            peaks.append(snap_x)
        band_peaks.append(peaks)

    # Theil-Sen fit per gutter → x at page center
    ys = np.array(band_ys, dtype=float)
    y_center = float(top + ch // 2)
    refined = []
    for gi in range(len(initial_gutters)):
        xs = np.array([bp[gi] for bp in band_peaks], dtype=float)
        pairwise = []
        for i in range(len(ys)):
            for j in range(i + 1, len(ys)):
                if ys[j] != ys[i]:
                    pairwise.append((xs[j] - xs[i]) / (ys[j] - ys[i]))
        if pairwise:
            slope = float(np.median(pairwise))
            intercept = float(np.median(xs - slope * ys))
            refined.append(int(round(slope * y_center + intercept)))
        else:
            refined.append(initial_gutters[gi])
    return refined


def _gutter_profile(img_gray, content_bounds):
    """
    Build a composite gutter-detection profile that captures the printed
    newspaper column-border pattern: whitespace | dark rule line | whitespace.

    Combines two signals via geometric mean:
      1. Darkness contrast: how much darker is each x-column vs its ±15px
         local background?  Rule lines spike high.
      2. Flanking brightness: are the columns ±3-6px away brighter than
         this one?  True gutter rule lines have whitespace on both sides;
         text columns do not.

    Returns (gutter_score, brightness) in LOCAL coords (0 = content left).
    """
    left, top, right, bot = content_bounds
    cw, ch = right - left, bot - top

    # Average brightness across 4 vertical zones (20-80% of page body)
    zone_specs = [(20, 35), (35, 50), (50, 65), (65, 80)]
    profiles = []
    for pct_s, pct_e in zone_specs:
        zt = top + ch * pct_s // 100
        zb = top + ch * pct_e // 100
        profiles.append(
            img_gray[zt:zb, left:right].mean(axis=0).astype(float))
    composite = np.mean(profiles, axis=0)
    n = len(composite)

    # Signal 1: local darkness contrast (rule line darker than neighbors)
    local_bg = uniform_filter1d(composite, size=30)
    darkness_dip = uniform_filter1d(local_bg - composite, size=3)

    # Signal 2: flanking brightness (whitespace on both sides)
    flank_score = np.zeros(n)
    for x in range(5, n - 5):
        left_flank  = composite[x - 5 : x - 1].mean()
        right_flank = composite[x + 2 : x + 6].mean()
        flank_score[x] = min(left_flank, right_flank) - composite[x]

    # Normalize each to [0, 1] and combine via geometric mean
    d_max = darkness_dip.max()
    f_max = flank_score.max()
    d_norm = np.clip(darkness_dip / d_max, 0, 1) if d_max > 0 else np.zeros(n)
    f_norm = np.clip(flank_score / f_max, 0, 1) if f_max > 0 else np.zeros(n)
    gutter_score = np.sqrt(d_norm * f_norm)

    return gutter_score, composite


def _score_equidistant(gutter_score, cw, N):
    """
    Score an N-column hypothesis by placing N-1 equidistant gutters and
    measuring how strong the gutter-pattern signal is at each position
    relative to the column centers on either side.

    Returns (total_contrast, min_contrast, snapped_local_xs) where xs are
    in LOCAL coordinates (0 = left edge of content).
    """
    spacing = cw / N
    window = max(8, int(spacing * 0.15))    # search ±15% of column width
    half_col = int(spacing * 0.40)           # sample 40% into column

    contrasts = []
    snapped = []
    for k in range(1, N):
        gx = int(spacing * k)
        lo = max(0, gx - window)
        hi = min(len(gutter_score), gx + window + 1)
        snap_x = lo + int(np.argmax(gutter_score[lo:hi]))
        peak_val = float(gutter_score[snap_x])
        snapped.append(snap_x)

        # Background: gutter score at column centers on either side
        left_c  = max(0, snap_x - half_col)
        right_c = min(len(gutter_score) - 1, snap_x + half_col)
        bg = max(float(gutter_score[left_c]), float(gutter_score[right_c]))
        contrasts.append(peak_val - bg)

    return sum(contrasts), min(contrasts), snapped


def _detect_gutters_equidistant(img_gray, content_bounds, n_cols,
                                rule_lines: dict = None) -> list:
    """
    Place gutters for a known N-column layout.  Primary signal: vertical
    rule lines from `_detect_rule_lines()`.  Fallback: equidistant placement
    snapped to peaks in the gutter-pattern profile (whitespace-rule-whitespace).

    Args:
        n_cols: number of columns (determined by cross-page consensus)

    Returns list of gutter x-coordinates (in page coords, at the rule line
    center — the dark line between whitespace strips).
    """
    if not HAS_CV2:
        return []
    left, top, right, bot = content_bounds
    cw = right - left
    ch = bot - top
    if cw < 200 or ch < 200:
        return []

    # ── Primary: use rule lines if available ──────────────────────────────
    if rule_lines and rule_lines.get("vertical"):
        v_rules = rule_lines["vertical"]
        edge_margin = cw // 15
        gutters = [x for x in v_rules
                   if x > left + edge_margin and x < right - edge_margin]
        if len(gutters) == n_cols - 1:
            tprint(f"      gutters from rule lines: {len(gutters)} at "
                   f"x={gutters}", level=3)
            return sorted(gutters)
        tprint(f"      rule lines: {len(gutters)} found but need "
               f"{n_cols - 1} — using gutter profile", level=4)

    # ── Equidistant placement snapped to gutter-pattern peaks ────────────
    # After deskew, rule lines are vertical so a single x-position per
    # gutter is correct.  The equidistant hypothesis constrains the search
    # to the right neighborhood; the gutter-pattern profile finds the
    # exact rule line center within that neighborhood.
    gscore, _ = _gutter_profile(img_gray, content_bounds)
    _, _, snapped_local = _score_equidistant(gscore, cw, n_cols)
    gutters = sorted(x + left for x in snapped_local)

    tprint(f"      equidistant ({n_cols} cols): gutters at x={gutters}",
           level=4)
    return gutters


def _h_rule_score_strip(img_gray, x_lo, x_hi, y_lo, y_hi):
    """
    Horizontal rule-line score for a single column strip.
    Same whitespace-dark-whitespace pattern used for vertical gutters,
    but applied row-wise: a printed horizontal border is a dark row
    flanked by whitespace above and below.

    Returns score array in LOCAL y-coords (0 = y_lo).
    """
    strip = img_gray[y_lo:y_hi, x_lo:x_hi]
    row_bright = strip.mean(axis=1).astype(float)
    n = len(row_bright)
    if n < 20:
        return np.zeros(n)
    local_bg = uniform_filter1d(row_bright, size=25)
    darkness = uniform_filter1d(local_bg - row_bright, size=2)
    flank = np.zeros(n)
    for y in range(4, n - 4):
        above = row_bright[y - 4 : y - 1].mean()
        below = row_bright[y + 2 : y + 5].mean()
        flank[y] = min(above, below) - row_bright[y]
    d_max = max(float(darkness.max()), 1e-6)
    f_max = max(float(flank.max()), 1e-6)
    d_norm = np.clip(darkness / d_max, 0, 1)
    f_norm = np.clip(flank / f_max, 0, 1)
    return np.sqrt(d_norm * f_norm)


def _detect_h_borders(img_gray, content_bounds, gutter_xs, n_cols):
    """
    Detect horizontal rule lines (borders) and determine which columns
    each one spans.

    Newspapers use horizontal rules to delimit the masthead, section
    breaks, ad boundaries, and footers.  An ad border starts and ends
    on column boundaries (gutters) and need not span the full page width.

    Strategy: compute the horizontal rule-line score independently for
    each column strip, find peaks per column, then cluster peaks across
    columns at the same y.  A border is confirmed when ≥2 adjacent
    columns agree.

    Returns list of dicts:
      {y, col_start (1-based), col_end (1-based), full_width: bool}
    sorted by y.
    """
    if not HAS_CV2:
        return []
    left, top, right, bot = content_bounds
    cw, ch = right - left, bot - top
    if cw < 200 or ch < 200:
        return []

    # Column boundaries (in page coords)
    col_bounds = [left] + list(gutter_xs) + [right]

    # Compute h-rule score per column
    col_peaks = []
    for ci in range(n_cols):
        x_lo, x_hi = col_bounds[ci], col_bounds[ci + 1]
        # Shrink strip slightly to avoid gutter rule interference
        margin = max(3, (x_hi - x_lo) // 10)
        score = _h_rule_score_strip(
            img_gray, x_lo + margin, x_hi - margin, top, bot)
        peaks, _ = find_peaks(score, height=0.25, prominence=0.12,
                              distance=8)
        col_peaks.append(set(int(p) for p in peaks))

    # Cluster peaks across columns at the same y (±tolerance)
    tol = 5
    all_ys = sorted(set().union(*col_peaks))
    visited = set()
    raw_borders = []

    for y in all_ys:
        if y in visited:
            continue
        cols_with = []
        for ci, peaks in enumerate(col_peaks):
            for p in peaks:
                if abs(p - y) <= tol:
                    cols_with.append(ci + 1)  # 1-based
                    visited.add(p)
                    break
        if len(cols_with) >= 2:
            raw_borders.append((y + top, sorted(cols_with)))

    # Merge borders within 12px
    merged = []
    for y, cols in raw_borders:
        if merged and y - merged[-1][0] < 12:
            combined = sorted(set(merged[-1][1] + cols))
            merged[-1] = (y, combined)
        else:
            merged.append((y, cols))

    # Filter: require ≥2 ADJACENT columns (not scattered coincidences).
    # A real horizontal border spans a contiguous run of columns.
    borders = []
    for y, cols in merged:
        # Find longest contiguous run
        best_start, best_end = cols[0], cols[0]
        run_start = cols[0]
        for i in range(1, len(cols)):
            if cols[i] == cols[i - 1] + 1:
                if cols[i] - run_start + 1 > best_end - best_start + 1:
                    best_start, best_end = run_start, cols[i]
            else:
                run_start = cols[i]
        contiguous_len = best_end - best_start + 1
        if contiguous_len < 2:
            continue
        full = contiguous_len >= n_cols - 1
        borders.append({
            "y": y,
            "col_start": best_start,
            "col_end": best_end,
            "full_width": full,
        })

    tprint(f"      h_borders: {len(borders)} detected "
           f"({sum(1 for b in borders if b['full_width'])} full-width, "
           f"{sum(1 for b in borders if not b['full_width'])} partial)",
           level=4)
    return borders


def _build_page_zones(content_bounds, gutter_xs, n_cols, h_borders):
    """
    Build rectangular zones from the vertical gutter grid and horizontal
    borders.  Classifies each zone as masthead, column, ad, or footer.

    The page is a grid defined by:
      - Vertical: content edges + column gutters
      - Horizontal: content edges + horizontal rule lines

    Zone classification rules:
      - masthead: spans all columns, in top 15% of page
      - footer: spans all columns, in bottom 12% of page
      - ad: bounded above and below by horizontal rules, spans < all columns
      - column: normal column body text (everything else)

    Returns (masthead, body_top, footer_top, zones) where:
      masthead: (l, t, r, mast_bottom) or None
      body_top: y where article columns start
      footer_top: y where footer starts (or bot if no footer)
      zones: list of zone dicts
    """
    left, top, right, bot = content_bounds
    ch = bot - top

    # Separate full-width borders from partial borders
    fw_borders = sorted([b for b in h_borders if b["full_width"]],
                        key=lambda b: b["y"])
    partial_borders = sorted([b for b in h_borders if not b["full_width"]],
                             key=lambda b: b["y"])

    # ── Masthead: first full-width border in top 15% ──────────────────
    # The masthead is separated from the body by a full-width horizontal
    # rule line.  Use the first such line near the top.  If there's
    # a second full-width line close below it (<5% of page height),
    # prefer the lower one (the masthead may have internal rules between
    # title, date line, and motto).
    masthead = None
    body_top = top
    mast_limit = top + ch * 15 // 100
    mast_candidates = [b for b in fw_borders
                       if b["y"] < mast_limit and b["y"] - top >= 15]
    if mast_candidates:
        # Use first candidate, but extend if next is very close
        chosen = mast_candidates[0]
        for c in mast_candidates[1:]:
            if c["y"] - chosen["y"] < ch * 5 // 100:
                chosen = c  # extend to include internal rule
            else:
                break
        masthead = (left, top, right, chosen["y"])
        body_top = chosen["y"]

    # ── Footer: last full-width border in bottom 15% ────────────────────
    footer_top = bot
    footer_limit = top + ch * 85 // 100
    for b in reversed(fw_borders):
        if b["y"] > footer_limit:
            footer_top = b["y"]
            break

    # ── Build zones ─────────────────────────────────────────────────────
    col_bounds = [left] + list(gutter_xs) + [right]
    zones = []

    # Masthead zone
    if masthead:
        zones.append({
            "type": "masthead",
            "bbox": masthead,
            "col_span": (1, n_cols),
        })

    # Ad zones from horizontal borders.
    # An ad is bounded by a top border and a bottom border that share
    # column span (overlap).  Borders can be full-width or partial.
    #
    # Two-phase strategy (order matters):
    #   Phase 1: Pair partial borders with a full-width border above.
    #            This catches ads that start at a section boundary
    #            (masthead bottom, section break).  Run first because
    #            these are higher-confidence pairings.
    #   Phase 2: Pair remaining partial borders with overlapping col spans.
    all_borders = sorted(fw_borders + partial_borders, key=lambda b: b["y"])
    ad_zones = []
    used = set()  # indices into all_borders

    # Phase 1: partial borders paired with the masthead bottom.
    # Catches ads that start immediately below the masthead (e.g.,
    # "Der Deutsche Tag!" spanning cols 4-6 on page 1).
    #
    # Guard: the ad must NOT start at column 1.  A partial border
    # starting at col 1 that pairs with the masthead would cover
    # the entire left portion of the page — that's article text,
    # not an ad.  Real masthead-adjacent ads start at interior columns.
    if masthead:
        for i, b in enumerate(all_borders):
            if b["full_width"] or b["y"] <= body_top or b["y"] > footer_top:
                continue
            gap = b["y"] - body_top
            n_span = b["col_end"] - b["col_start"] + 1
            if (30 < gap < ch * 40 // 100
                    and n_span < n_cols
                    and b["col_start"] > 1):
                cs, ce = b["col_start"], b["col_end"]
                x1, x2 = col_bounds[cs - 1], col_bounds[ce]
                ad_zones.append({
                    "type": "ad",
                    "bbox": (x1, body_top, x2, b["y"]),
                    "col_span": (cs, ce),
                })
                used.add(i)

    # Phase 2: pair remaining partial borders with MATCHING col spans.
    # Require that the top and bottom borders span the same columns
    # (within 1 column tolerance).  This prevents section-break rules
    # (which span varying widths) from being paired as ad boxes.
    partial_idxs = [i for i, b in enumerate(all_borders)
                    if not b["full_width"] and i not in used
                    and body_top <= b["y"] <= footer_top]
    for pi, i in enumerate(partial_idxs):
        if i in used:
            continue
        b_top = all_borders[i]
        for pj in range(pi + 1, len(partial_idxs)):
            j = partial_idxs[pj]
            if j in used:
                continue
            b_bot = all_borders[j]
            # Require exact matching span: both borders must cover the
            # same columns.  Section-break rules have ragged detection
            # that spans different column counts; real ad boxes have
            # precise, consistent top and bottom borders.
            start_match = b_top["col_start"] == b_bot["col_start"]
            end_match = b_top["col_end"] == b_bot["col_end"]
            span_lo = max(b_top["col_start"], b_bot["col_start"])
            span_hi = min(b_top["col_end"], b_bot["col_end"])
            gap = b_bot["y"] - b_top["y"]
            # Also reject if there's a full-width border between the
            # top and bottom — that indicates a section break, meaning
            # the two partial borders bound different content regions.
            fw_between = any(fb["y"] > b_top["y"] and fb["y"] < b_bot["y"]
                             for fb in fw_borders)
            if (start_match and end_match and span_lo <= span_hi
                    and 15 < gap < ch // 2 and not fw_between):
                x1 = col_bounds[span_lo - 1]
                x2 = col_bounds[span_hi]
                ad_zones.append({
                    "type": "ad",
                    "bbox": (x1, b_top["y"], x2, b_bot["y"]),
                    "col_span": (span_lo, span_hi),
                })
                used.add(i)
                used.add(j)
                break

    # Filter: minimum ad height (5% of page height).  Tiny "ads"
    # are usually just pairs of nearby section-break rules.
    min_ad_h = max(40, ch * 5 // 100)
    ad_zones = [a for a in ad_zones
                if a["bbox"][3] - a["bbox"][1] >= min_ad_h]

    # Deduplicate overlapping ads: when multiple ads share the same
    # starting y and column span, keep only the tightest (smallest area).
    # This happens when Phase 1 pairs multiple partial borders below
    # the same full-width border.
    deduped = []
    ad_zones.sort(key=lambda a: (a["bbox"][1], a["col_span"],
                                 a["bbox"][3] - a["bbox"][1]))
    for ad in ad_zones:
        # Check if this ad is contained within an existing one
        contained = False
        for existing in deduped:
            eb = existing["bbox"]
            ab = ad["bbox"]
            # Same or overlapping start, one contains the other
            if (ab[0] >= eb[0] and ab[2] <= eb[2] and
                    ab[1] >= eb[1] and ab[3] <= eb[3]):
                contained = True
                break
            # Same start, same cols, different height — keep smaller
            if (ab[1] == eb[1] and ad["col_span"] == existing["col_span"]):
                contained = True
                break
        if not contained:
            # Also remove any existing ads that this one contains
            deduped = [e for e in deduped if not (
                e["bbox"][0] >= ad["bbox"][0] and
                e["bbox"][2] <= ad["bbox"][2] and
                e["bbox"][1] >= ad["bbox"][1] and
                e["bbox"][3] <= ad["bbox"][3])]
            deduped.append(ad)

    # Merge adjacent ads: ads are typically butted against each other.
    # If two ads in the same (or overlapping) columns are separated by
    # a small gap (< 50px ≈ 1-10 lines of text), merge them into one.
    # This also catches borders that were incorrectly detected within
    # a single continuous ad.
    merge_gap = max(50, ch * 4 // 100)
    deduped.sort(key=lambda a: (a["col_span"], a["bbox"][1]))
    merged_ads = []
    for ad in deduped:
        merged = False
        for i, existing in enumerate(merged_ads):
            # Same column span and small vertical gap?
            if ad["col_span"] == existing["col_span"]:
                eg = existing["bbox"]
                ag = ad["bbox"]
                gap = ag[1] - eg[3]  # top of new - bottom of existing
                if 0 <= gap <= merge_gap:
                    # Merge: extend existing ad downward
                    merged_ads[i] = {
                        "type": "ad",
                        "bbox": (eg[0], eg[1], eg[2], ag[3]),
                        "col_span": existing["col_span"],
                    }
                    merged = True
                    break
        if not merged:
            merged_ads.append(ad)

    zones.extend(merged_ads)

    # Column zones (body columns between masthead and footer)
    for ci in range(n_cols):
        x1, x2 = col_bounds[ci], col_bounds[ci + 1]
        if x2 - x1 >= 30:
            zones.append({
                "type": "column",
                "index": ci + 1,
                "bbox": (x1, body_top, x2, footer_top),
            })

    # Footer zone
    if footer_top < bot - 20:
        zones.append({
            "type": "footer",
            "bbox": (left, footer_top, right, bot),
            "col_span": (1, n_cols),
        })

    return masthead, body_top, footer_top, zones


def analyze_page_layout(img_gray, page_num: int, content_bounds: tuple,
                        n_cols: int = 0,
                        override_gutters: list = None) -> dict:
    """
    Analyze a single page's layout by building a 2D grid from vertical
    column gutters and horizontal rule-line borders, then classifying
    each rectangular zone.

    Newspaper pages are a perpendicular grid:
      - Vertical: column gutters (rule lines between columns)
      - Horizontal: masthead border, section borders, ad borders, footer

    Ads interrupt vertical columns: they have horizontal borders at
    top and bottom, and begin/end aligned to column gutters (spanning
    1+ columns).

    If override_gutters is provided (from cross-page median), uses those
    gutter positions directly.  If n_cols > 0, detects per-page gutters.
    Otherwise falls back to per-page best-guess (scoring pass).

    Returns a layout dict with:
      - gutter_xs, n_cols: the vertical column grid
      - h_borders: detected horizontal borders with column-span info
      - masthead, body_top, footer_top: page-level boundaries
      - zones: list of classified rectangular zones
      - columns: the column zones (for backward compat with OCR pipeline)
    """
    left, top, right, bot = content_bounds

    # Step 1: Detect printed rule lines (reliable for thick borders)
    rule_lines = _detect_rule_lines(img_gray, content_bounds)

    # Step 2: Detect vertical gutters
    if override_gutters:
        gutter_xs = list(override_gutters)
        n_cols = len(gutter_xs) + 1
    elif n_cols > 0:
        gutter_xs = _detect_gutters_equidistant(
            img_gray, content_bounds, n_cols, rule_lines=rule_lines)
    else:
        gscore, _ = _gutter_profile(img_gray, content_bounds)
        cw = right - left
        best_n, best_score, best_gutters = 3, -999, []
        for cand_n in range(3, 9):
            total, mn, snapped = _score_equidistant(gscore, cw, cand_n)
            if total > best_score:
                best_score, best_n = total, cand_n
                best_gutters = [x + left for x in snapped]
        gutter_xs = sorted(best_gutters)
        n_cols = best_n

    # Step 3: Detect horizontal borders (masthead, section, ad, footer)
    h_borders = _detect_h_borders(img_gray, content_bounds,
                                  gutter_xs, n_cols)

    # Step 4: Build zones from the perpendicular grid
    masthead, body_top, footer_top, zones = _build_page_zones(
        content_bounds, gutter_xs, n_cols, h_borders)

    # Step 5: Optional deep-learning layout detection (LayoutParser)
    # Supplements geometric zones with semantic labels (Advertisement,
    # Headline, Figure, etc.) when available.
    if HAS_LAYOUTPARSER:
        lp_regions = _layoutparser_detect(img_gray)
        if lp_regions:
            zones = _merge_lp_zones(zones, lp_regions, content_bounds)

    # Extract column zones for backward compat with OCR pipeline
    columns = [z for z in zones if z["type"] == "column"]

    return {
        "page_num": page_num,
        "content_bounds": content_bounds,
        "rule_lines": rule_lines,
        "gutter_xs": gutter_xs,
        "n_cols": n_cols,
        "h_borders": h_borders,
        "masthead": masthead,
        "body_top": body_top,
        "footer_top": footer_top,
        "columns": columns,
        "zones": zones,
    }


def render_layout_overlay(img_gray, layout: dict, page_num: int) -> bytes:
    """
    Render a debug overlay image with colored bboxes for each detected zone.
    Returns PNG bytes. Colors:
      Red = masthead, Green = columns, Blue = ads, Yellow = separators
    """
    if not HAS_CV2:
        return b""

    # Convert grayscale to BGR for colored overlay
    overlay = cv2.cvtColor(img_gray, cv2.COLOR_GRAY2BGR)
    alpha = 0.3

    for zone in layout["zones"]:
        ztype = zone["type"]
        color = ZONE_COLORS.get(ztype, ZONE_COLORS["unknown"])
        x1, y1, x2, y2 = zone["bbox"]

        # Semi-transparent filled rectangle
        sub = overlay[y1:y2, x1:x2].copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
        overlay[y1:y2, x1:x2] = cv2.addWeighted(overlay[y1:y2, x1:x2], alpha, sub, 1 - alpha, 0)

        # Border
        cv2.rectangle(overlay, (x1, y1), (x2, y2), color, 2)

        # Label
        label = ztype
        if ztype == "column":
            label = f"col {zone.get('index', '?')}"
        elif ztype == "ad":
            cs = zone.get("col_span", ("?", "?"))
            label = f"ad c{cs[0]}-{cs[1]}"
        cv2.putText(overlay, label, (x1 + 4, y1 + 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

    # Page number and column count
    n_ads = sum(1 for z in layout["zones"] if z["type"] == "ad")
    cv2.putText(overlay,
                f"Page {page_num}  ({layout['n_cols']} cols, {n_ads} ads)",
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

    l, t, r, b = layout["content_bounds"]

    # Draw gutters (yellow-green, the confirmed column dividers)
    for gx in layout["gutter_xs"]:
        cv2.line(overlay, (gx, t), (gx, b), (0, 255, 255), 2)

    # Draw horizontal borders (cyan, with thickness reflecting type)
    for hb in layout.get("h_borders", []):
        y = hb["y"]
        col_bounds = [l] + list(layout["gutter_xs"]) + [r]
        x1 = col_bounds[hb["col_start"] - 1]
        x2 = col_bounds[hb["col_end"]]
        thickness = 2 if hb["full_width"] else 1
        cv2.line(overlay, (x1, y), (x2, y), (255, 255, 0), thickness)

    # Legend
    legend_y = b + 15 if b + 30 < img_gray.shape[0] else t - 15
    n_hb = len(layout.get("h_borders", []))
    cv2.putText(overlay,
                f"Gutters: {len(layout['gutter_xs'])} | "
                f"H-borders: {n_hb} | Ads: {n_ads}",
                (10, legend_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                (200, 200, 200), 1)

    _, png = cv2.imencode(".png", overlay)
    return png.tobytes()


# ============================================================================
# OPTIONAL: LayoutParser / Detectron2 deep-learning layout detection
# ============================================================================
# When installed (pip install layoutparser torchvision detectron2), provides
# CNN-based region detection (Faster/Mask R-CNN trained on PubLayNet or
# Newspaper Navigator) as an additional signal for zone classification.
#
# Integration philosophy: same as Kraken — try-import, lazy-load model,
# return [] on error.  LayoutParser provides region-level boxes with labels
# (Text, Title, Table, Figure, List) that SUPPLEMENT our geometric detection,
# not replace it.  The geometric pipeline (gutters, h_borders, gutter-pattern)
# is the primary structure; LayoutParser adds semantic labels.
#
# Setup:
#   pip install layoutparser torchvision
#   pip install 'git+https://github.com/facebookresearch/detectron2.git'
#   # Or for Newspaper Navigator model:
#   pip install layoutparser[detectron2]

def _layoutparser_detect(img_gray) -> list:
    """
    Run LayoutParser detection on a page image.

    Returns list of dicts:
      {bbox: (x1, y1, x2, y2), label: str, score: float}

    Labels depend on the model (PubLayNet: Text/Title/List/Table/Figure;
    Newspaper Navigator: Headline/Advertisement/Illustration/...).

    Returns [] if LayoutParser is not installed or detection fails.
    """
    global _LP_MODEL
    if not HAS_LAYOUTPARSER:
        return []
    try:
        if _LP_MODEL is None:
            # Try Newspaper Navigator model first (better for newspapers),
            # fall back to PubLayNet (more general).
            # Each entry: (config_path, label_map)
            model_configs = [
                (
                    "lp://NewspaperNavigator/faster_rcnn_R_50_FPN_3x/config",
                    {0: "Photograph", 1: "Illustration", 2: "Map",
                     3: "Comic", 4: "Editorial_Cartoon",
                     5: "Headline", 6: "Advertisement"},
                ),
                (
                    "lp://PubLayNet/mask_rcnn_R_50_FPN_3x/config",
                    {0: "Text", 1: "Title", 2: "List",
                     3: "Table", 4: "Figure"},
                ),
            ]
            for config, label_map in model_configs:
                try:
                    _LP_MODEL = lp.Detectron2LayoutModel(
                        config_path=config,
                        label_map=label_map,
                        extra_config=[
                            "MODEL.ROI_HEADS.SCORE_THRESH_TEST", 0.5],
                    )
                    tprint(f"  LayoutParser model loaded: {config}",
                           level=1)
                    break
                except Exception as model_err:
                    tprint(f"  LayoutParser model {config}: {model_err}",
                           level=3)
                    continue
            if _LP_MODEL is None:
                tprint("  ⚠ LayoutParser: no model could be loaded", level=1)
                return []

        # Convert grayscale to RGB (LayoutParser expects color)
        if len(img_gray.shape) == 2:
            img_rgb = cv2.cvtColor(img_gray, cv2.COLOR_GRAY2RGB)
        else:
            img_rgb = img_gray

        layout = _LP_MODEL.detect(img_rgb)
        results = []
        for block in layout:
            x1, y1, x2, y2 = map(int, block.coordinates)
            results.append({
                "bbox": (x1, y1, x2, y2),
                "label": block.type,
                "score": float(block.score),
            })
        tprint(f"      LayoutParser: {len(results)} regions detected", level=3)
        return results
    except Exception as e:
        tprint(f"      ⚠ LayoutParser error: {e}", level=2)
        return []


def _merge_lp_zones(zones: list, lp_regions: list,
                    content_bounds: tuple) -> list:
    """
    Merge LayoutParser detections into existing geometric zones.

    Strategy:
      - LP "Advertisement" regions → if they overlap a column zone,
        split that column zone and insert an ad zone.
      - LP "Headline"/"Title" regions → tag overlapping zones.
      - LP "Figure"/"Photograph"/"Illustration" → add as image zones.

    This is a soft merge: LP provides labels/confidence, geometric
    detection provides precise boundaries.  When they agree, confidence
    is high.  When they disagree, the geometric structure wins (LP
    models may not be trained on this specific newspaper style).
    """
    if not lp_regions:
        return zones

    left, top, right, bot = content_bounds
    new_zones = list(zones)

    for lr in lp_regions:
        lx1, ly1, lx2, ly2 = lr["bbox"]
        label = lr["label"]
        score = lr["score"]

        if label in ("Advertisement",) and score >= 0.6:
            # Check if this overlaps with any column zone
            for i, z in enumerate(new_zones):
                if z["type"] != "column":
                    continue
                zx1, zy1, zx2, zy2 = z["bbox"]
                # Compute overlap
                ox1 = max(lx1, zx1)
                oy1 = max(ly1, zy1)
                ox2 = min(lx2, zx2)
                oy2 = min(ly2, zy2)
                if ox1 < ox2 and oy1 < oy2:
                    overlap_area = (ox2 - ox1) * (oy2 - oy1)
                    lp_area = (lx2 - lx1) * (ly2 - ly1)
                    if overlap_area > lp_area * 0.3:
                        # Significant overlap — add as ad zone
                        new_zones.append({
                            "type": "ad",
                            "bbox": (ox1, oy1, ox2, oy2),
                            "col_span": (z.get("index", 1),
                                         z.get("index", 1)),
                            "lp_score": score,
                        })

        elif label in ("Figure", "Photograph", "Illustration",
                        "Map", "Comic", "Editorial_Cartoon"):
            if score >= 0.5:
                new_zones.append({
                    "type": "figure",
                    "bbox": lr["bbox"],
                    "lp_label": label,
                    "lp_score": score,
                })

    return new_zones


def analyze_issue_layout(ark_id: str, pages_to_analyze: list,
                         worker_id: str = "") -> tuple:
    """
    Stage 0: Analyze layout of all pages in an issue.

    Three-pass approach:
      Pass 0 — Load all page images.  Estimate skew from vertical rule
               lines (Theil-Sen fit across 10 narrow bands).  Deskew all
               pages if the tilt exceeds 0.2°.  Rule lines in printed
               newspapers are always straight and perpendicular to the
               horizontal borders — any deviation is scan skew.
      Pass 1 — Score column-count hypotheses (N=3..8) across all pages.
               For each N, places equidistant gutters and measures the
               whitespace-rule_line-whitespace gutter pattern signal.
               Picks the N with the highest cross-page consensus.
      Pass 2 — Re-analyze every page with the winning N, snapping
               equidistant gutters to actual rule-line positions, then
               refining via multi-band straight-line fitting.
               Also detects mastheads and produces debug overlays.

    Returns:
      expected_cols: int — consensus column count
      page_layouts:  dict of page_num → layout dict

    Also saves debug overlay images to artifacts/layout/{ark_id}/.
    """
    tprint(f"  ┌─ Stage 0: Layout analysis ───────────────────────────────",
           worker=worker_id, level=1)

    # ── Pass 0: load images and deskew if needed ─────────────────────────
    page_images = {}   # pg → (img_gray, bounds)

    for pg in pages_to_analyze:
        img_bytes, _ = fetch_page_image(ark_id, pg)
        if not img_bytes:
            tprint(f"  │  p{pg:02d} ⚠ no image", worker=worker_id, level=1)
            continue
        nparr    = np.frombuffer(img_bytes, np.uint8)
        img_gray = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        bounds   = detect_content_bounds(img_gray)
        page_images[pg] = (img_gray, bounds)

    # Estimate skew from the first few pages with a preliminary N=6 guess.
    # Rule lines are always perfectly straight — any measured tilt is scan
    # skew.  Median across pages is robust to front-page oddities.
    skew_estimates = []
    for pg in list(page_images)[:min(4, len(page_images))]:
        img_gray, bounds = page_images[pg]
        skew = _estimate_skew(img_gray, bounds, n_cols=6)
        skew_estimates.append(skew)

    skew_deg = float(np.median(skew_estimates)) if skew_estimates else 0.0
    if abs(skew_deg) >= 0.2:
        tprint(f"  │  deskew: {skew_deg:+.2f}° detected, rotating all pages",
               worker=worker_id, level=1)
        for pg in page_images:
            img_gray, _ = page_images[pg]
            img_gray = _deskew_image(img_gray, skew_deg)
            bounds = detect_content_bounds(img_gray)
            page_images[pg] = (img_gray, bounds)
    else:
        tprint(f"  │  deskew: {skew_deg:+.2f}° (within tolerance, no rotation)",
               worker=worker_id, level=3)

    # ── Cross-page content bounds: most inclusive ───────────────────────
    # All pages of the same issue are the same physical page scanned on
    # the same microfilm.  Tattered/torn edges may cause per-page
    # detection to be too conservative (excluding content).  Use the
    # MOST INCLUSIVE bounds across all pages: minimum left/top (to include
    # content at torn edges) and maximum right/bottom.  Content at torn
    # edges may be degraded but is still OCR-valuable.
    if len(page_images) >= 3:
        all_bounds = [b for _, b in page_images.values()]
        best_left  = int(np.min([b[0] for b in all_bounds]))
        best_top   = int(np.min([b[1] for b in all_bounds]))
        best_right = int(np.max([b[2] for b in all_bounds]))
        best_bot   = int(np.max([b[3] for b in all_bounds]))
        for pg in page_images:
            img_gray, old_bounds = page_images[pg]
            h, w = img_gray.shape
            new_bounds = (
                max(0, best_left),
                max(0, best_top),
                min(w, best_right),
                min(h, best_bot),
            )
            if new_bounds != old_bounds:
                tprint(f"  │  p{pg:02d} bounds adjusted: "
                       f"left {old_bounds[0]}→{new_bounds[0]}  "
                       f"right {old_bounds[2]}→{new_bounds[2]}",
                       worker=worker_id, level=4)
            page_images[pg] = (img_gray, new_bounds)

    # ── Pass 1: score column-count hypotheses ────────────────────────────
    cross_scores = {}  # N → list of (total_contrast, min_contrast) per page

    for pg in page_images:
        img_gray, bounds = page_images[pg]
        gscore, _ = _gutter_profile(img_gray, bounds)
        cw = bounds[2] - bounds[0]
        for N in range(3, 9):
            total, mn, _ = _score_equidistant(gscore, cw, N)
            cross_scores.setdefault(N, []).append((total, mn))

    # Pick the N with best cross-page consensus.
    # Score = sum of total_contrast across pages × fraction of pages where
    # the weakest gutter still has positive contrast (all gutters real).
    best_n, best_consensus = 5, -999
    for N in sorted(cross_scores):
        scores = cross_scores[N]
        total_sum = sum(t for t, m in scores)
        pages_ok = sum(1 for t, m in scores if m > 0)
        consensus = total_sum * (pages_ok / max(1, len(scores)))
        marker = ""
        if consensus > best_consensus:
            best_consensus = consensus
            best_n = N
            marker = " ← best"
        tprint(f"  │  score N={N}: total={total_sum:7.1f}  "
               f"pages_ok={pages_ok}/{len(scores)}  "
               f"consensus={consensus:7.1f}{marker}",
               worker=worker_id, level=3)

    expected_cols = best_n
    tprint(f"  │  consensus: {expected_cols} columns",
           worker=worker_id, level=1)

    # ── Pass 2: compute per-page gutters and take cross-page median ────
    # Column gutters are at fixed ABSOLUTE positions on every page of an
    # issue (mechanically typeset).  Using absolute median positions
    # (not relative fractions) ensures that tattered/torn edges on
    # individual pages don't shift the column grid.
    per_page_gutters = []  # list of [x1, ..., xN-1] per page
    for pg in pages_to_analyze:
        if pg not in page_images:
            continue
        img_gray, bounds = page_images[pg]
        gutter_xs = _detect_gutters_equidistant(
            img_gray, bounds, expected_cols)
        per_page_gutters.append(gutter_xs)

    # Median ABSOLUTE gutter positions across pages
    median_gutters = []
    if per_page_gutters:
        for gi in range(expected_cols - 1):
            vals = [pg_g[gi] for pg_g in per_page_gutters
                    if gi < len(pg_g)]
            median_gutters.append(int(np.median(vals)))
        tprint(f"  │  median gutters (absolute): {median_gutters}",
               worker=worker_id, level=3)

    # ── Pass 3: finalize layouts using median gutter positions ───────────
    page_layouts = {}
    for pg in pages_to_analyze:
        if pg not in page_images:
            continue
        img_gray, bounds = page_images[pg]

        layout = analyze_page_layout(img_gray, pg, bounds,
                                     n_cols=expected_cols,
                                     override_gutters=median_gutters)
        layout["skew_deg"] = skew_deg
        page_layouts[pg] = layout

        mast_str = ""
        if layout["masthead"]:
            mh = layout["masthead"]
            mast_str = f"  masthead={mh[3]-mh[1]}px tall"
        tprint(f"  │  p{pg:02d}: {layout['n_cols']} columns, "
               f"{len(layout['gutter_xs'])} gutters{mast_str}",
               worker=worker_id, level=1)
        if LOG_LEVEL >= 4:
            for z in layout["zones"]:
                b = z["bbox"]
                tprint(f"  │    {z['type']}: ({b[0]},{b[1]})→({b[2]},{b[3]}) "
                       f"{b[2]-b[0]}x{b[3]-b[1]}px", level=4)

        # Save debug overlay
        try:
            artifacts_dir = CORRECTED_DIR.parent / "artifacts" / "layout" / ark_id
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            overlay_png = render_layout_overlay(img_gray, layout, pg)
            if overlay_png:
                (artifacts_dir / f"page_{pg:02d}_layout.png").write_bytes(overlay_png)
                tprint(f"  │    overlay → artifacts/layout/{ark_id}/"
                       f"page_{pg:02d}_layout.png", level=3)
        except Exception as e:
            tprint(f"  │    ⚠ overlay save failed: {e}", level=3)

    tprint(f"  └─ Layout: {expected_cols} columns (cross-page consensus)",
           worker=worker_id, level=1)

    return expected_cols, page_layouts


def save_calibration(collection_dir: Path, ark_id: str,
                     expected_cols: int, page_layouts: dict):
    """
    Save auto-detected layout as calibration.json for user review/correction.
    The user can edit this file manually or use the GUI tool to adjust.
    """
    # Get skew from first layout that has it
    skew_deg = 0.0
    for layout in page_layouts.values():
        if "skew_deg" in layout:
            skew_deg = layout["skew_deg"]
            break
    cal = {
        "version": CALIBRATION_VERSION,
        "calibrated_from": ark_id,
        "calibrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "expected_cols": expected_cols,
        "skew_deg": skew_deg,
        "tuning_params": {
            "rule_line_threshold": 0.55,
            "rule_line_contrast": 0.12,
            "min_column_width": 80,
            "prominence": 0.15,
        },
        "page_layouts": {},
    }
    for pg, layout in sorted(page_layouts.items()):
        entry = {"user_corrected": False, "n_cols": layout["n_cols"]}
        if layout.get("masthead"):
            entry["masthead"] = list(layout["masthead"])
        entry["columns"] = [list(c["bbox"]) for c in layout.get("columns", [])]
        entry["gutter_xs"] = layout.get("gutter_xs", [])
        rules = layout.get("rule_lines", {})
        if rules:
            entry["rule_lines_v"] = rules.get("vertical", [])
            entry["rule_lines_h"] = rules.get("horizontal", [])
        cal["page_layouts"][str(pg)] = entry

    cal_path = collection_dir / "calibration.json"
    cal_path.write_text(json.dumps(cal, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    tprint(f"  Calibration saved → {cal_path}", level=1)
    tprint(f"  Review overlay images in artifacts/layout/{ark_id}/ and edit", level=1)
    tprint(f"  calibration.json to correct any wrong boundaries.", level=1)
    return cal_path


CALIBRATION_VERSION = 2  # bump to invalidate stale calibration files


def load_calibration(collection_dir: Path) -> dict | None:
    """Load calibration.json if it exists and version matches. Returns None
    if not found or if the calibration was produced by an older algorithm
    version (will be re-detected automatically)."""
    cal_path = collection_dir / "calibration.json"
    if not cal_path.exists():
        return None
    try:
        cal = json.loads(cal_path.read_text(encoding="utf-8"))
        if cal.get("version", 0) < CALIBRATION_VERSION:
            tprint(f"  Calibration v{cal.get('version', 0)} is outdated "
                   f"(need v{CALIBRATION_VERSION}) — re-detecting layout",
                   level=1)
            return None
        tprint(f"  Calibration loaded from {cal_path} "
               f"(calibrated from {cal.get('calibrated_from', '?')})", level=1)
        return cal
    except Exception as e:
        tprint(f"  ⚠ Failed to load calibration: {e}", level=1)
        return None


def _layout_from_calibration(cal_page: dict, page_num: int,
                              content_bounds: tuple,
                              skew_deg: float = 0.0) -> dict:
    """Convert a calibration.json page entry back to a page_layout dict."""
    left, top, right, bot = content_bounds
    masthead = tuple(cal_page["masthead"]) if cal_page.get("masthead") else None
    body_top = masthead[3] if masthead else top

    columns = []
    for i, bbox in enumerate(cal_page.get("columns", []), 1):
        columns.append({"type": "column", "index": i, "bbox": tuple(bbox)})

    zones = []
    if masthead:
        zones.append({"type": "masthead", "bbox": masthead})
    zones.extend(columns)

    return {
        "page_num": page_num,
        "content_bounds": content_bounds,
        "rule_lines": {
            "vertical": cal_page.get("rule_lines_v", []),
            "horizontal": cal_page.get("rule_lines_h", []),
        },
        "gutter_xs": cal_page.get("gutter_xs", []),
        "n_cols": cal_page.get("n_cols", len(columns)),
        "skew_deg": skew_deg,
        "masthead": masthead,
        "body_top": body_top,
        "columns": columns,
        "zones": zones,
    }


# ============================================================================
# STAGE 2 — PREPROCESSING
# ============================================================================

def preprocess_image(img_gray):
    """CLAHE contrast enhancement + gentle despeckling for microfilm."""
    if not HAS_CV2:
        return img_gray
    clahe    = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    enhanced = clahe.apply(img_gray)
    return cv2.medianBlur(enhanced, 3)


def detect_content_bounds(img_gray):
    """
    Trim dark microfilm borders and torn/tattered edges.
    Returns (left, top, right, bottom).

    Uses a sustained-brightness approach: the content edge is where
    the column/row mean brightness first stays above threshold for
    a run of consecutive pixels (not just the first bright pixel).
    This avoids tattered edges where brightness fluctuates before
    the actual printed content begins.
    """
    if not HAS_CV2:
        h, w = img_gray.shape
        return int(w*.05), int(h*.02), int(w*.97), int(h*.98)
    h, w = img_gray.shape
    _, bw = cv2.threshold(img_gray, 60, 255, cv2.THRESH_BINARY)

    # Use the MIDDLE 50% of the page for column brightness (avoids
    # masthead/footer regions that can mask torn edges).
    mid_top = h * 25 // 100
    mid_bot = h * 75 // 100
    cl = np.mean(bw[mid_top:mid_bot, :], axis=0)

    # Use the MIDDLE 50% for row brightness (avoids left/right edge)
    mid_left = w * 15 // 100
    mid_right = w * 85 // 100
    rl = np.mean(bw[:, mid_left:mid_right], axis=1)

    # Find where brightness sustains above threshold for ≥8 consecutive
    # columns/rows.  On torn edges, brightness flickers in and out;
    # real content has sustained brightness.
    threshold = 140
    run_needed = 8

    def _find_sustained(profile, forward=True):
        n = len(profile)
        rng = range(n) if forward else range(n - 1, -1, -1)
        run = 0
        for i in rng:
            if profile[i] > threshold:
                run += 1
                if run >= run_needed:
                    return i - (run_needed - 1) if forward else i + (run_needed - 1)
            else:
                run = 0
        # Fallback: first bright pixel
        for i in rng:
            if profile[i] > 120:
                return i
        return 0 if forward else n - 1

    left  = _find_sustained(cl, forward=True) + 3
    right = _find_sustained(cl, forward=False) - 3
    top   = _find_sustained(rl, forward=True) + 3
    bot   = _find_sustained(rl, forward=False) - 3
    return max(0, left), max(0, top), min(w, right), min(h, bot)


# ============================================================================
# STAGE 3 — COLUMN DETECTION (always from image)
# ============================================================================

def detect_columns_from_image(img_gray, content_bounds, expected_cols: int = 5) -> list:
    """
    Find column gutters using vertical dark-pixel projection on the
    bottom 30% of the content area.

    Always runs from the image — independent of ABBYY.
    Returns list of (x_start, x_end) tuples, left to right.
    """
    left, top, right, bot = content_bounds
    cw = right - left
    ch = bot - top

    if not HAS_CV2 or cw < 200:
        w = cw // expected_cols
        return [(left + i*w, left + (i+1)*w) for i in range(expected_cols)]

    # Bottom 30%: below mastheads/multi-column announcements
    zt = top + ch * 7 // 10
    zb = top + ch * 95 // 100
    region   = img_gray[zt:zb, left:right]
    dark     = (region < 128).sum(axis=0).astype(float)
    smoothed = uniform_filter1d(dark, size=12)

    valleys, props = find_peaks(-smoothed, distance=80,
                                prominence=smoothed.max() * 0.05)

    n_gutters = expected_cols - 1
    if len(valleys) >= n_gutters:
        idx       = np.argsort(-props["prominences"])[:n_gutters]
        gutter_xs = sorted(int(valleys[i]) + left for i in idx)
    elif len(valleys) > 0:
        gutter_xs = sorted(int(v) + left for v in valleys)
    else:
        w = cw // expected_cols
        return [(left + i*w, left + (i+1)*w) for i in range(expected_cols)]

    splits  = [left] + gutter_xs + [right]
    columns = [(splits[i], splits[i+1]) for i in range(len(splits) - 1)]
    return [(x1, x2) for x1, x2 in columns if x2 - x1 >= 50]


# ============================================================================
# STAGE 4 — BOUNDARY COMPARISON (when ABBYY available)
# ============================================================================

def compare_boundaries(opencv_cols: list, abbyy_gutters: list,
                       snap_tolerance: int = 20,
                       expected_cols: int = 5) -> tuple:
    """
    Reconcile OpenCV-detected column gutters with ABBYY block boundary evidence
    into a final column layout.

    Returns:
      final_columns — reconciled (x_start, x_end) list
      report        — human-readable string of every decision made

    Decision rules (applied in order):
      1. OpenCV gutter within snap_tolerance of an ABBYY gutter
         → snap to ABBYY position (keeps both, ABBYY has precise block coords)
         → mark ABBYY gutter as accounted for

      2. OpenCV gutter with NO nearby ABBYY gutter
         → keep it, but tag as "OpenCV-only, unverified"
         → a single unverified gutter in a 5-column newspaper is likely real;
           if there are many, something is wrong with detection

      3. ABBYY gutter with NO nearby OpenCV gutter
         → INSERT it into the final gutter set
         → ABBYY had sub-pixel block coordinates from FineReader; if it found
           a column boundary that OpenCV missed, that boundary is almost
           certainly real (OpenCV misses narrow columns and gutters in zones
           dominated by a large headline block)

    Why we trust ABBYY misses over OpenCV misses:
      ABBYY FineReader assigns block boundaries from a layout analysis pass
      that explicitly models newspaper columns. OpenCV projection-based detection
      can be confused by masthead spanning text even when we use the bottom 30%
      zone. An ABBYY boundary with no OpenCV support is a missed gutter.
      An OpenCV boundary with no ABBYY support is more likely a spurious
      detection from a large text block or image region.
    """
    if not abbyy_gutters:
        return opencv_cols, "No ABBYY boundary data — using OpenCV only"

    report_lines    = []
    opencv_gutter_xs = sorted(x2 for _, x2 in opencv_cols[:-1])
    left             = opencv_cols[0][0]
    right            = opencv_cols[-1][1]
    abbyy_remaining  = list(abbyy_gutters)   # track which ABBYY gutters are unaccounted for

    final_gutter_xs = []

    # Pass 1: process each OpenCV gutter
    opencv_only = []    # unverified OpenCV gutters — candidates for removal
    for ocx in opencv_gutter_xs:
        # Find nearest ABBYY gutter
        if abbyy_remaining:
            closest = min(abbyy_remaining, key=lambda ax: abs(ax - ocx))
            dist    = abs(closest - ocx)
        else:
            closest, dist = None, 9999

        if dist <= snap_tolerance:
            # Agree — snap to ABBYY
            final_gutter_xs.append(closest)
            abbyy_remaining.remove(closest)
            report_lines.append(
                f"  AGREE    OpenCV={ocx} ↔ ABBYY={closest} (Δ{dist}px) → using {closest}")
        else:
            # OpenCV-only — keep tentatively, will review after ABBYY inserts
            final_gutter_xs.append(ocx)
            opencv_only.append(ocx)
            report_lines.append(
                f"  OPENCV   x={ocx} unverified (nearest ABBYY={closest}, Δ{dist}px)")

    # Pass 2: insert ABBYY gutters that OpenCV never found
    for ax in sorted(abbyy_remaining):
        if left < ax < right:
            too_close = any(abs(ax - fx) <= snap_tolerance for fx in final_gutter_xs)
            if not too_close:
                final_gutter_xs.append(ax)
                report_lines.append(
                    f"  INSERTED ABBYY x={ax} — OpenCV missed this gutter, adding it")
            else:
                report_lines.append(
                    f"  SKIPPED  ABBYY x={ax} — too close to existing gutter, duplicate")

    # Pass 3: prune excess OpenCV-only gutters
    # After inserting ABBYY gutters we may have more gutters than expected.
    # Any excess almost certainly comes from spurious OpenCV detections —
    # ABBYY rarely produces phantom gutters, but projection-based detection
    # can find false valleys under large headline blocks.
    #
    # Strategy: if we have more gutters than expected_cols-1, remove the
    # OpenCV-only ones in order of how far they are from any ABBYY gutter
    # (furthest = least supported = most likely spurious).
    n_gutters_now = len(final_gutter_xs)
    n_gutters_expected = expected_cols - 1

    if n_gutters_now > n_gutters_expected and opencv_only:
        excess = n_gutters_now - n_gutters_expected
        # Sort opencv_only by distance from nearest ABBYY gutter (descending)
        def dist_from_abbyy(ocx):
            if not abbyy_gutters:
                return 0
            return min(abs(ocx - ax) for ax in abbyy_gutters)
        candidates = sorted(opencv_only, key=dist_from_abbyy, reverse=True)
        to_remove  = candidates[:excess]
        for ocx in to_remove:
            if ocx in final_gutter_xs:
                final_gutter_xs.remove(ocx)
                report_lines.append(
                    f"  REMOVED  OpenCV x={ocx} — {excess} excess gutter(s) vs expected "
                    f"{n_gutters_expected}; this one has least ABBYY support "
                    f"(Δ{dist_from_abbyy(ocx)}px from nearest ABBYY)")

    # Build final columns from merged gutter set
    all_gutters = sorted(final_gutter_xs)
    splits      = [left] + all_gutters + [right]
    final_cols  = [(splits[i], splits[i+1]) for i in range(len(splits) - 1)]
    final_cols  = [(x1, x2) for x1, x2 in final_cols if x2 - x1 >= 50]

    summary = "\n".join(report_lines) if report_lines else "Boundaries fully agree"
    return final_cols, summary


# ============================================================================
# STAGE 5 — TESSERACT (word tokens with confidence + bbox)
# ============================================================================

def detect_tesseract_lang() -> str | None:
    """Best available Tesseract language for German Fraktur."""
    if not HAS_TESSERACT:
        return None
    try:
        available = pytesseract.get_languages()
    except Exception:
        return None
    for lang in TESS_LANG_PRIORITY:
        if all(p in available for p in lang.split("+")):
            return lang
    return None


def tesseract_tokens(col_img, lang: str, config: str, source_tag: str) -> list:
    """
    Run Tesseract on one column strip with image_to_data.
    Returns list of WordToken dicts with text, conf, bbox, source.
    Words with conf < 10 become ILLEGIBLE. conf < TESS_CONF_MIN are disputed.
    """
    if not HAS_TESSERACT or not lang:
        return []
    try:
        tprint(f"      {source_tag}: lang={lang} config='{config}' "
               f"img={col_img.shape[1]}x{col_img.shape[0]}px", level=3)
        pil  = PILImage.fromarray(col_img)
        data = pytesseract.image_to_data(pil, lang=lang, config=config,
                                         output_type=pytesseract.Output.DICT)
        tokens = []
        for i in range(len(data["text"])):
            text = data["text"][i].strip()
            conf = int(data["conf"][i])
            if not text or conf < 0:
                continue
            tok_text = ILLEGIBLE if conf < 10 else text
            tokens.append({
                "text":   tok_text,
                "conf":   conf,
                "source": source_tag,
                "left":   data["left"][i],
                "top":    data["top"][i],
                "right":  data["left"][i] + data["width"][i],
                "bottom": data["top"][i]  + data["height"][i],
            })
        tprint(f"      {source_tag}: → {len(tokens)} tokens", level=4)
        if tokens and LOG_LEVEL >= 5:
            sample = [t["text"] for t in tokens[:10]]
            tprint(f"      {source_tag} sample: {' '.join(sample)}...", level=5)
        return tokens
    except Exception as e:
        tprint(f"    ⚠ Tesseract ({source_tag}) error: {e}", level=2)
        return []


# ============================================================================
# STAGE 6 — KRAKEN (optional third engine)
# ============================================================================

# Best Kraken model for German Fraktur newspapers (Austrian Newspapers ground truth)
_KRAKEN_MODEL_URL  = "https://zenodo.org/records/7933402/files/austriannewspapers.mlmodel?download=1"
_KRAKEN_MODEL_NAME = "austriannewspapers.mlmodel"


def _kraken_model_dir() -> Path:
    """Return the platform-specific directory for storing Kraken models."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA", "")
        return Path(base) / "kraken" if base else Path.home() / ".kraken"
    xdg = os.environ.get("XDG_DATA_HOME", "")
    return Path(xdg) / "kraken" if xdg else Path.home() / ".local" / "share" / "kraken"


def _find_kraken_model() -> str | None:
    """
    Find a Kraken .mlmodel file. Searches:
      1. Our own model directory (kraken/ under platform data dir)
      2. htrmopo directories (kraken ≥4.x model store)
      3. Legacy ~/.kraken/
    """
    search_dirs = [_kraken_model_dir()]

    # htrmopo directories (where `kraken get` stores models)
    if sys.platform == "win32":
        for var in ("LOCALAPPDATA", "APPDATA"):
            d = os.environ.get(var, "")
            if d: search_dirs.append(Path(d) / "htrmopo")
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg: search_dirs.append(Path(xdg) / "htrmopo")
        search_dirs.append(Path.home() / ".local" / "share" / "htrmopo")

    search_dirs.append(Path.home() / ".kraken")

    for d in search_dirs:
        if not d.exists():
            continue
        models = list(d.rglob("*.mlmodel"))
        if models:
            # Prefer austriannewspapers model if present, else most recent
            for m in models:
                if "austriannewspapers" in m.name.lower():
                    return str(m)
            models.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return str(models[0])
    return None


def _download_kraken_model() -> str:
    """Download the Fraktur-optimized Kraken model from Zenodo."""
    dest_dir = _kraken_model_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / _KRAKEN_MODEL_NAME

    tprint(f"  Downloading Kraken model → {dest}", level=1)
    tprint(f"    URL: {_KRAKEN_MODEL_URL.split('?')[0]}", level=1)
    req = urllib.request.Request(_KRAKEN_MODEL_URL,
                                headers={"User-Agent": "UNT-Archive/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    dest.write_bytes(data)
    tprint(f"    ✓ {len(data) // 1024:,} KB downloaded", level=1)
    return str(dest)


_kraken_load_lock = threading.Lock()


def kraken_tokens(col_img, source_tag: str = "kraken") -> list:
    """
    Run Kraken on a column strip if installed.
    Returns WordToken list in same format as tesseract_tokens.
    Requires: pip install kraken
    """
    global _KRAKEN_MODEL, HAS_KRAKEN
    if not HAS_KRAKEN:
        return []
    try:
        if _KRAKEN_MODEL is None:
            with _kraken_load_lock:
                if _KRAKEN_MODEL is None:
                    model_path = _find_kraken_model()
                    if not model_path:
                        model_path = _download_kraken_model()
                    _KRAKEN_MODEL = kraken_models.load_any(model_path)
                    tprint(f"  Kraken model: {model_path}", level=3)
        pil = PILImage.fromarray(col_img)
        # Suppress ALL Kraken output during segmentation + recognition.
        # Polygonizer warnings go to stdout/stderr/logging unpredictably.
        import warnings, logging, io, contextlib
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            logging.getLogger("kraken").setLevel(logging.CRITICAL)
            devnull = io.StringIO()
            with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                seg = blla.segment(pil)
                records = list(rpred.rpred(_KRAKEN_MODEL, pil, seg))
        tokens = []
        for record in records:
            text = record.prediction.strip()
            if not text:
                continue
            conf = int(record.confidences[0] * 100) if record.confidences else 50
            # Extract bounding box from record — API varies by Kraken version:
            # BBoxOCRRecord has .bbox; BaselineOCRRecord has .boundary (polygon)
            x0 = y0 = x1 = y1 = 0
            try:
                if hasattr(record, 'bbox') and record.bbox:
                    # Legacy BBoxOCRRecord
                    x0, y0, x1, y1 = record.bbox
                elif hasattr(record, 'boundary') and record.boundary:
                    # BaselineOCRRecord: boundary is a polygon [(x,y), ...]
                    xs = [p[0] for p in record.boundary]
                    ys = [p[1] for p in record.boundary]
                    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
                elif hasattr(record, 'baseline') and record.baseline:
                    # Fallback: derive from baseline endpoints
                    xs = [p[0] for p in record.baseline]
                    ys = [p[1] for p in record.baseline]
                    x0, y0 = min(xs), min(ys) - 10
                    x1, y1 = max(xs), max(ys) + 10
                else:
                    continue
            except Exception:
                continue
            tokens.append({
                "text":   ILLEGIBLE if conf < 10 else text,
                "conf":   conf,
                "source": source_tag,
                "left": int(x0), "top": int(y0),
                "right": int(x1), "bottom": int(y1),
            })
        tprint(f"      kraken: → {len(tokens)} tokens from {len(records)} lines", level=4)
        if tokens and LOG_LEVEL >= 5:
            sample = [t["text"] for t in tokens[:10]]
            tprint(f"      kraken sample: {' '.join(sample)}...", level=5)
        return tokens
    except Exception as e:
        err = str(e).lower()
        if "model" in err or "not loadable" in err or ".mlmodel" in err:
            HAS_KRAKEN = False
            tprint(f"  ⚠ Kraken disabled for this run: {e}", level=1)
        else:
            tprint(f"    ⚠ Kraken error: {e}", level=2)
        return []


# ============================================================================
# STAGE 7 — WORD ALIGNMENT + AGREEMENT ANALYSIS
# ============================================================================

def align_sources(sources: dict, pos_tolerance: int = 15) -> list:
    """
    Align word tokens from multiple OCR sources by bounding-box proximity.

    sources: {"abbyy": [token,...], "tess_a": [token,...], "tess_b": [...], ...}
    Each source may be absent; we handle any subset gracefully.

    Returns list of AlignedWord dicts:
      {
        "tokens":    {"abbyy": tok|None, "tess_a": tok|None, ...},
        "agree":     bool,    # True if all present sources match + conf OK
        "consensus": str,     # best reading, or ILLEGIBLE
        "dispute_reason": str # human-readable, empty if agree
      }

    Anchor source priority: abbyy > tess_a > tess_b > kraken
    We align all others to the anchor by center-point distance.
    """
    anchor_priority = ["abbyy", "tess_a", "tess_b", "kraken"]
    anchor_name = next((k for k in anchor_priority if k in sources), None)
    if anchor_name is None:
        return []

    anchor_toks  = sources[anchor_name]
    other_srcs   = {k: list(v) for k, v in sources.items() if k != anchor_name}

    aligned = []
    for at in anchor_toks:
        ac = ((at["left"] + at["right"]) // 2,
              (at["top"]  + at["bottom"]) // 2)
        group = {anchor_name: at}

        for src_name, src_toks in other_srcs.items():
            best, best_d = None, 9999
            for st in src_toks:
                sc = ((st["left"] + st["right"]) // 2,
                      (st["top"]  + st["bottom"]) // 2)
                d  = abs(ac[0] - sc[0]) + abs(ac[1] - sc[1])
                if d < best_d and d < pos_tolerance * 3:
                    best, best_d = st, d
            group[src_name] = best

        # ── Agreement analysis ───────────────────────────────────────────
        present = {k: t for k, t in group.items() if t is not None}
        readings = {}
        for k, t in present.items():
            r = t["text"].strip()
            if r:
                readings[k] = r

        unique = set(r.lower() for r in readings.values())
        confs  = [t["conf"] for t in present.values()]
        min_conf = min(confs) if confs else 0

        agree   = False
        reason  = ""
        consensus = ILLEGIBLE

        if not readings:
            reason    = "all sources empty"
        elif all(r == ILLEGIBLE for r in readings.values()):
            reason    = "all sources illegible"
        elif len(unique) == 1 and unique != {ILLEGIBLE.lower()}:
            # All present sources agree on same non-illegible reading
            if min_conf >= TESS_CONF_MIN:
                agree     = True
                consensus = readings[anchor_name]   # preserves capitalisation
            else:
                reason    = f"agree on text but min_conf={min_conf} < {TESS_CONF_MIN}"
                consensus = readings[anchor_name]
        else:
            # Sources disagree — pick highest-confidence reading as provisional
            best_src  = max(present.keys(), key=lambda k: present[k]["conf"])
            consensus = readings.get(best_src, ILLEGIBLE)
            diff_srcs = [f"{k}={readings[k]!r}(c={present[k]['conf']})"
                         for k in sorted(readings)]
            reason    = "sources disagree: " + "  ".join(diff_srcs)

        aligned.append({
            "tokens":         group,
            "agree":          agree,
            "consensus":      consensus,
            "dispute_reason": reason,
        })

    return aligned


def split_agree_dispute(aligned: list) -> tuple:
    """
    Split aligned words into agreed text and a dispute table for Claude.

    Returns:
      agreed_text_lines — list of strings, one per OCR line.
                          Agreed words appear as-is.
                          Disputed words appear as {?provisional?} so Claude
                          can see exactly where each dispute falls in context.
      dispute_table     — list of dicts, one per disputed word position:
                          {top, left, provisional, readings, confs, reason,
                           line_context}   ← surrounding line for reference

    The {?...?} markers in agreed_text_lines let Claude see the disputed
    positions in context without needing a separate coordinate lookup.
    Claude replaces each {?provisional?} with its resolution in the output.
    """
    # Group words into lines by top coordinate proximity (within 12px = same line)
    lines          = []
    cur_line_top   = None
    cur_line_words = []

    for word in aligned:
        # Get top coordinate from the anchor token (first key in tokens dict)
        anchor_tok = next(iter(word["tokens"].values()))
        tok_top    = anchor_tok["top"] if anchor_tok else 0

        if cur_line_top is None or abs(tok_top - cur_line_top) > 12:
            if cur_line_words:
                lines.append(cur_line_words)
            cur_line_words = [word]
            cur_line_top   = tok_top
        else:
            cur_line_words.append(word)
    if cur_line_words:
        lines.append(cur_line_words)

    agreed_text_lines = []
    dispute_table     = []

    for line_words in lines:
        line_parts = []
        for w in line_words:
            if w["agree"]:
                line_parts.append(w["consensus"])
            else:
                # Mark disputed words visibly so Claude can find them
                line_parts.append(f"{{?{w['consensus']}?}}")
        line_str = " ".join(line_parts)
        agreed_text_lines.append(line_str)

        # Build dispute entries for each non-agreed word in this line
        for w in line_words:
            if not w["agree"]:
                anchor_tok = next(iter(w["tokens"].values()))
                dispute_table.append({
                    "top":          anchor_tok["top"]    if anchor_tok else 0,
                    "left":         anchor_tok["left"]   if anchor_tok else 0,
                    "right":        anchor_tok["right"]  if anchor_tok else 0,
                    "bottom":       anchor_tok["bottom"] if anchor_tok else 0,
                    "provisional":  w["consensus"],
                    "readings":     {k: (t["text"] if t else "(none)")
                                     for k, t in w["tokens"].items()},
                    "confs":        {k: (t["conf"] if t else 0)
                                     for k, t in w["tokens"].items()},
                    "reason":       w["dispute_reason"],
                    "line_context": line_str,   # the full line for reference
                })

    return agreed_text_lines, dispute_table


# ============================================================================
# STAGE 8 — CLAUDE ARBITRATION
# ============================================================================

def build_correction_prompt(config: dict) -> str:
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

    fraktur = ""
    if "fraktur" in typeface.lower():
        fraktur = f"""
FRAKTUR OCR ERROR PATTERNS — use to resolve disputes:
  b/d:  ber→der  bie→die  bas→das  unb→und  bon→von  burch→durch
        bann→dann  boch→doch  bies→dies  babei→dabei
  f/s:  fein→sein  fie→sie  fo→so  fich→sich  finb→sind
        foll→soll  werben→werden  wurbe→wurde
  3/Z:  3u→Zu  3um→Zum  3eit→Zeit  3eitung→Zeitung
  j/z:  ju→zu  jum→zum  jwei→zwei
  J/I:  Jm→Im  Jch→Ich  Jn→In  Jhr→Ihr
  G/E:  Gr→Er  Gs→Es  Gin→Ein  Gine→Eine
  cf→ck: zurücf→zurück  Glücf→Glück  Stücf→Stück
  ß:    da«→daß  mu«→muß  (angle quotes → ß)
  -h:   sic→sich  nac→nach  auc→auch  noc→noch
  =/-:  Wissen=\\nschaft → Wissenschaft  (rejoin broken compounds)
"""

    ctx = ""
    if community: ctx += f"\nCOMMUNITY: {community}"
    if history:   ctx += f"\nHISTORY: {history}"
    if subjects:  ctx += f"\nSUBJECTS: {subjects}"
    if places:    ctx += f"\nPLACE NAMES (preserve exactly): {places}"
    if orgs:      ctx += f"\nORGANIZATIONS (preserve exactly): {orgs}"

    return f"""You are an expert in historical German newspaper OCR correction.

COLLECTION: {title}
Publisher: {publisher} | Location: {location} | Period: {date_range}
Language: {language} | Typeface: {typeface} | Source: {source}
{f"LCCN: {lccn}" if lccn else ""}
{ctx}
{fraktur}
YOUR ROLE
=========
Multiple OCR engines have already processed this page. Words where all
engines agree and confidence is high are accepted automatically — you
never see those. You only receive:

  1. The agreed text (column-ordered, with [Column N] markers).
     This is mostly clean — spot-check it, fix obvious compound-word
     breaks and systematic Fraktur errors you notice.

  2. A DISPUTE TABLE listing every word where engines disagreed or
     confidence was low. Each entry shows every engine's reading and
     confidence. Use the page scan image to determine the correct reading.

DISPUTE RESOLUTION RULES:
  • If you can read the word clearly in the image → use that reading
  • If the image is ambiguous → pick the most plausible German word
    given context and Fraktur error patterns
  • If genuinely unreadable in image → use exactly: {ILLEGIBLE}
  • Rejoin compound words broken at line ends (Wissen=\\nschaft → Wissenschaft)
  • Do not invent content; do not translate

OUTPUT FORMAT:
Return a single JSON object, no markdown fences:
{{
  "corrected_text": "<full corrected page text with [Column N] markers>",
  "resolutions": {{
    "<provisional>|<top>": "<your resolved reading or {ILLEGIBLE}>"
  }}
}}

In corrected_text: replace every {{?word?}} marker with your resolved reading.
If you cannot resolve a {{?word?}} from the image → use {ILLEGIBLE} exactly.
Do not leave any {{?...?}} markers in corrected_text.
The resolutions dict is for audit logging — include every dispute you resolved."""


def claude_api_call(payload: dict, api_key: str,
                    rate_limiter=None, est_tokens: int = 8000) -> str:
    """Shared Claude API call with retry/rate-limit handling."""
    tprint(f"    → Claude API: model={payload.get('model')} "
           f"max_tokens={payload.get('max_tokens')} est={est_tokens}", level=3)
    if LOG_LEVEL >= 5:
        sys_prompt = payload.get("system", "")
        tprint(f"      system prompt: {len(sys_prompt)} chars, "
               f"first 100: {sys_prompt[:100]}...", level=5)
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
            in_tok  = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            if rate_limiter:
                rate_limiter.record_usage(input_tokens=in_tok, output_tokens=out_tok)
            tprint(f"    ← Claude API: {in_tok} in + {out_tok} out tokens", level=3)
            for block in result.get("content", []):
                if block.get("type") == "text":
                    return block["text"].strip()
            return ""
        except urllib.error.HTTPError as e:
            if e.code in (429, 503, 529):
                wait = 30 * attempt
                print(f" [rate limit {e.code}, wait {wait}s]", end="", flush=True)
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            if attempt < 3:
                time.sleep(15 * attempt)
            else:
                raise
    raise RuntimeError("Max retries exceeded")


def arbitrate_with_claude(ark_id: str, page_num: int, total_pages: int,
                          agreed_text: str, dispute_table: list,
                          issue_meta: dict, api_key: str,
                          correction_prompt: str,
                          rate_limiter=None) -> str:
    """
    Send page image + agreed text + dispute table to Claude.
    Returns corrected full page text.

    If there are no disputes, Claude still gets the agreed text to catch
    compound-word breaks and systematic errors the alignment missed.
    """
    img_bytes, img_type = fetch_page_image(ark_id, page_num)

    content = []
    if img_bytes:
        content.append({"type": "image", "source": {
            "type":       "base64",
            "media_type": img_type or "image/jpeg",
            "data":       base64.standard_b64encode(img_bytes).decode("ascii"),
        }})
        img_note = "Page scan attached above."
    else:
        img_note = "NOTE: Page image unavailable — resolve from OCR text only."

    # Build dispute table block (capped at 300 entries to limit token use)
    dispute_block = ""
    if dispute_table:
        rows = []
        for d in dispute_table[:300]:
            readings_str = "  ".join(
                f"{src}={r!r}(c={d['confs'].get(src, 0)})"
                for src, r in sorted(d["readings"].items()))
            rows.append(
                f"  {{?{d['provisional']}?}}  →  {readings_str}")
        dispute_block = (
            f"\nDISPUTE TABLE ({len(dispute_table)} positions"
            f"{', showing first 300' if len(dispute_table) > 300 else ''}):\n"
            f"Each row shows the {{?marker?}} as it appears in the agreed text,\n"
            f"followed by each engine's reading and confidence score.\n"
            + "\n".join(rows))

    prompt = (
        f"ISSUE: {issue_meta.get('full_title', '')}\n"
        f"DATE:  {issue_meta.get('date', '')}\n"
        f"PAGE:  {page_num} of {total_pages}  ARK: {ark_id}\n\n"
        f"{img_note}\n\n"
        f"AGREED TEXT — column-ordered, with {{?disputed?}} markers for uncertain words:\n"
        f"{agreed_text}\n"
        f"{dispute_block}\n\n"
        f"Instructions:\n"
        f"1. Look up each {{?word?}} in the dispute table above.\n"
        f"2. Check the page image to determine the correct reading.\n"
        f"3. Return the full corrected_text with every {{?...?}} replaced.\n"
        f"4. Use {ILLEGIBLE} for anything genuinely unreadable.\n"
        f"5. Also fix any compound-word breaks you notice (e.g. Wissen=\\nschaft).\n"
        f"\nReturn the JSON object now."
    )
    content.append({"type": "text", "text": prompt})

    n_disputes = len(dispute_table)
    est = 3000 + n_disputes * 20
    raw = claude_api_call(
        {"model": CLAUDE_MODEL, "max_tokens": 6000,
         "system": correction_prompt,
         "messages": [{"role": "user", "content": content}]},
        api_key, rate_limiter, est_tokens=est)

    # Parse JSON response
    try:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M).strip()
        data  = json.loads(clean)
        result = data.get("corrected_text", "").strip()
        if not result:
            result = raw
    except Exception:
        result = raw

    # Safety net: if any {?...?} markers remain unreplaced (Claude didn't finish
    # or returned plain text instead of JSON), strip them to their provisional text
    result = re.sub(r'\{\?([^?}]*)\?\}', r'\1', result)

    return result


# ============================================================================
# ILLEGIBLE COORDINATE TAGGING
# ============================================================================

def _tag_illegible_with_bbox(corrected_text: str, dispute_table: list) -> str:
    """
    Replace bare [unleserlich] markers with coordinate-tagged versions.

    Format: [unleserlich bbox=x,y,w,h]
    Where x,y,w,h are page-absolute pixel coordinates of the region that
    could not be read. This enables future selective re-OCR of only the
    unreadable regions when better tools become available.

    Uses dispute table entries that have page-absolute coordinates
    (page_left, page_top, page_right, page_bottom) set by process_page().
    """
    # Build lookup of illegible disputes with page coords
    illegible_bboxes = []
    for d in dispute_table:
        prov = d.get("provisional", "")
        if prov == ILLEGIBLE or (d.get("confs") and max(d["confs"].values()) == 0):
            pl = d.get("page_left", 0)
            pt = d.get("page_top", 0)
            pr = d.get("page_right", pl + 30)
            pb = d.get("page_bottom", pt + 20)
            if pl > 0 or pt > 0:  # have real coords
                illegible_bboxes.append((pl, pt, pr - pl, pb - pt))

    if not illegible_bboxes:
        return corrected_text

    # Replace [unleserlich] occurrences with tagged versions, using coords
    # in order of appearance. If more markers than coords, extras stay bare.
    bbox_iter = iter(illegible_bboxes)
    def _replace_with_bbox(match):
        try:
            x, y, w, h = next(bbox_iter)
            return f"[unleserlich bbox={x},{y},{w},{h}]"
        except StopIteration:
            return match.group(0)  # no more coords, leave bare

    return re.sub(r'\[unleserlich\]', _replace_with_bbox, corrected_text)


# ============================================================================
# PER-PAGE ORCHESTRATOR
# ============================================================================

def process_page_local(ark_id: str, page_num: int, total_pages: int,
                       unt_ocr_text: str, issue_meta: dict,
                       tess_lang: str | None,
                       abbyy_page_tokens: list,
                       abbyy_blocks: list,
                       expected_cols: int = 5,
                       worker_id: str = "",
                       page_layout: dict = None) -> dict:
    """
    LOCAL stages only (1-7): image → preprocess → columns → Tesseract → Kraken → align.
    Returns a dict with all data needed for Claude (or a no-disputes fast path).

    If page_layout is provided (from Stage 0), uses its pre-detected column
    geometry instead of re-running column detection.

    Keys in returned dict:
      - 'agreed_text', 'all_disputes': raw alignment output
      - 'summary': human-readable pipeline summary string
      - 'img_bytes': page image bytes (needed by Claude in Stage 8)
      - 'unt_ocr_text': fallback OCR text
      - 'no_image': True if image was unavailable
    """
    result = {
        "page_num": page_num,
        "unt_ocr_text": unt_ocr_text,
        "no_image": False,
    }

    # ── Load image ────────────────────────────────────────────────────────
    img_bytes, _ = fetch_page_image(ark_id, page_num)
    if not img_bytes:
        tprint(f"    p{page_num:02d} ⚠ image unavailable", worker=worker_id, level=1)
        result["no_image"] = True
        result["agreed_text"] = unt_ocr_text[:3000]
        result["all_disputes"] = []
        result["img_bytes"] = None
        result["summary"] = "no-image"
        return result

    result["img_bytes"] = img_bytes
    nparr    = np.frombuffer(img_bytes, np.uint8)
    img_gray = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
    h, w = img_gray.shape
    tprint(f"      image loaded: {w}x{h}px ({len(img_bytes)//1024}KB)", level=3)

    # Deskew if layout analysis detected skew
    skew = page_layout.get("skew_deg", 0.0) if page_layout else 0.0
    if abs(skew) >= 0.2:
        img_gray = _deskew_image(img_gray, skew)
        tprint(f"      deskewed {skew:+.2f}°", level=3)

    # Stage 2: Preprocess
    tprint(f"      preprocessing (CLAHE + median blur) ...", level=3)
    enhanced = preprocess_image(img_gray)

    # Stage 3: Detect columns (use pre-analyzed layout if available)
    if page_layout and page_layout.get("columns"):
        tprint(f"      using pre-analyzed layout ({page_layout['n_cols']} cols)", level=3)
        bounds = page_layout["content_bounds"]
        left, top, right, bot = bounds
        # Extract column (x1, x2) pairs from layout zones
        opencv_cols = [(z["bbox"][0], z["bbox"][2]) for z in page_layout["columns"]]
        n_opencv = len(opencv_cols)
        # Use body_top from layout so OCR skips the masthead zone
        if page_layout.get("masthead"):
            top = page_layout["body_top"]
            tprint(f"      masthead detected — OCR starts at y={top}", level=3)
    else:
        tprint(f"      detecting columns ...", level=3)
        bounds = detect_content_bounds(img_gray)
        left, top, right, bot = bounds
        opencv_cols = detect_columns_from_image(img_gray, bounds, expected_cols=expected_cols)
        n_opencv = len(opencv_cols)

    tprint(f"      → {n_opencv} columns  "
           f"content=({left},{top})→({right},{bot})", level=3)
    if LOG_LEVEL >= 5:
        for ci, (cx1, cx2) in enumerate(opencv_cols, 1):
            tprint(f"        col {ci}: x={cx1}→{cx2} ({cx2-cx1}px wide)", level=5)

    # Stage 4: Boundary comparison (if ABBYY data present)
    final_cols = opencv_cols
    if abbyy_blocks:
        tprint(f"      comparing ABBYY boundaries ...", level=3)
        abbyy_gutters = abbyy_column_boundaries(abbyy_blocks)
        final_cols, boundary_report = compare_boundaries(
            opencv_cols, abbyy_gutters, expected_cols=expected_cols)
        tprint(f"      → ABBYY reconciled: {len(final_cols)} columns", level=3)
        if boundary_report and boundary_report != "Boundaries agree":
            tprint(f"      boundary detail:\n{boundary_report}", level=4)

    # Assign ABBYY tokens to columns
    abbyy_by_col: dict[int, list] = {}
    if abbyy_page_tokens:
        for tok in abbyy_page_tokens:
            tok_cx = (tok["left"] + tok["right"]) // 2
            for col_idx, (cx1, cx2) in enumerate(final_cols, 1):
                if cx1 <= tok_cx < cx2:
                    local = dict(tok)
                    local["left"]  = max(0, tok["left"]  - cx1)
                    local["right"] = max(0, tok["right"] - cx1)
                    abbyy_by_col.setdefault(col_idx, []).append(local)
                    break
        tprint(f"      ABBYY tokens: {sum(len(v) for v in abbyy_by_col.values())} "
               f"across {len(abbyy_by_col)} columns", level=3)

    # Stages 5-7: OCR each column → align → agree/dispute
    all_agreed_lines = []
    all_disputes     = []
    engines_used     = set()

    # Buffer: extend each column crop by ~2-3 characters on each side.
    # Characters straddling gutter boundaries get caught; the alignment
    # and arbitration steps naturally filter orphan characters.
    h_img, w_img = enhanced.shape[:2]

    for col_idx, (cx1, cx2) in enumerate(final_cols, 1):
        # Crop with buffer (clamped to image bounds)
        buf_x1 = max(0, cx1 - CROP_BUFFER_PX)
        buf_x2 = min(w_img, cx2 + CROP_BUFFER_PX)
        strip = enhanced[top:bot, buf_x1:buf_x2]
        # Offset from buffer start to actual column start (for coord mapping)
        buf_offset = cx1 - buf_x1

        if strip.shape[1] < 50:
            tprint(f"      col {col_idx}: too narrow ({strip.shape[1]}px), skipping", level=4)
            continue

        tprint(f"      col {col_idx}/{n_opencv}: OCR engines ...", level=3)
        sources: dict[str, list] = {}

        if col_idx in abbyy_by_col:
            sources["abbyy"] = abbyy_by_col[col_idx]
            engines_used.add("abbyy")
            tprint(f"        abbyy: {len(abbyy_by_col[col_idx])} tokens", level=4)

        if HAS_TESSERACT and tess_lang:
            ta = tesseract_tokens(strip, tess_lang, TESS_PSM_A, "tess_a")
            tb = tesseract_tokens(strip, tess_lang, TESS_PSM_B, "tess_b")
            if ta: sources["tess_a"] = ta; engines_used.add("tess_a")
            if tb: sources["tess_b"] = tb; engines_used.add("tess_b")

        if HAS_KRAKEN:
            tprint(f"        kraken: segmenting + recognizing ...", level=3)
            kr = kraken_tokens(strip)
            if kr: sources["kraken"] = kr; engines_used.add("kraken")

        if not sources:
            tprint(f"      col {col_idx}: no engine produced tokens", level=3)
            continue

        # Stage 7: Alignment
        src_summary = ", ".join(f"{k}={len(v)}" for k, v in sources.items())
        tprint(f"        aligning sources: {src_summary}", level=3)
        aligned           = align_sources(sources)
        agreed_lines, dis = split_agree_dispute(aligned)
        tprint(f"        → agreed={len(agreed_lines)} lines, "
               f"disputes={len(dis)}", level=3)

        if dis and LOG_LEVEL >= 5:
            for d in dis[:3]:
                readings = d.get("readings", {})
                tprint(f"          dispute: {d.get('provisional','?')} — "
                       f"readings={readings}", level=5)
            if len(dis) > 3:
                tprint(f"          ... and {len(dis)-3} more disputes", level=5)

        for d in dis:
            # Map from buffered-strip coords to page coords.
            # buf_x1 is the left edge of the buffered strip in page coords.
            d["page_left"]   = d["left"]   + buf_x1
            d["page_top"]    = d["top"]    + top
            d["page_right"]  = d.get("right",  d["left"] + 30) + buf_x1
            d["page_bottom"] = d.get("bottom", d["top"]  + 20) + top
            d["column"]      = col_idx

        all_agreed_lines.append(f"[Column {col_idx}]")
        all_agreed_lines.extend(agreed_lines)
        all_disputes.extend(dis)

    agreed_text = "\n".join(all_agreed_lines)
    if not agreed_text.strip():
        agreed_text = unt_ocr_text[:3000]
        tprint(f"      ⚠ no engine output — falling back to portal OCR", level=2)

    agree_count   = sum(1 for l in all_agreed_lines if l.strip() and not l.startswith("["))
    dispute_count = len(all_disputes)
    summary = (f"{n_opencv}cols  engines={','.join(sorted(engines_used))}  "
               f"agreed≈{agree_count}words  disputes={dispute_count}")

    result["agreed_text"]   = agreed_text
    result["all_disputes"]  = all_disputes
    result["summary"]       = summary
    return result


def process_page_claude(local_result: dict, ark_id: str, total_pages: int,
                        issue_meta: dict, api_key: str, correction_prompt: str,
                        rate_limiter=None) -> tuple:
    """
    CLAUDE stages (8): arbitrate disputes using the image + aligned text.
    Receives output from process_page_local().
    Returns (corrected_text, summary).
    """
    page_num     = local_result["page_num"]
    agreed_text  = local_result["agreed_text"]
    all_disputes = local_result["all_disputes"]
    summary      = local_result["summary"]

    # No image — Claude corrects text-only
    if local_result.get("no_image"):
        tprint(f"    p{page_num:02d} sending to Claude (no image, text-only correction)", level=1)
        raw = claude_api_call(
            {"model": CLAUDE_MODEL, "max_tokens": 6000,
             "system": correction_prompt,
             "messages": [{"role": "user", "content":
                 f"PAGE {page_num}: Correct this OCR text (no image available):\n{local_result['unt_ocr_text']}"}]},
            api_key, rate_limiter, est_tokens=3000)
        tprint(f"    p{page_num:02d} ← Claude returned {len(raw)} chars", level=3)
        return raw, "no-image"

    # No disputes — all engines agree, skip Claude
    if len(all_disputes) == 0 and agreed_text.strip():
        tprint(f"    p{page_num:02d} all engines agree — skipping Claude", level=1)
        corrected = re.sub(r'\{\?([^?}]*)\?\}', r'\1', agreed_text)
        corrected = _tag_illegible_with_bbox(corrected, all_disputes)
        summary += "  claude=skipped(no-disputes)"
        return corrected, summary

    # Stage 8: Claude arbitrates disputes
    tprint(f"    p{page_num:02d} sending {len(all_disputes)} disputes to Claude ...", level=1)
    if LOG_LEVEL >= 5:
        tprint(f"      agreed text: {len(agreed_text)} chars, "
               f"first 120: {agreed_text[:120]}...", level=5)
    corrected = arbitrate_with_claude(
        ark_id, page_num, total_pages,
        agreed_text, all_disputes,
        issue_meta, api_key, correction_prompt,
        rate_limiter=rate_limiter)
    tprint(f"    p{page_num:02d} ← Claude returned {len(corrected)} chars", level=3)

    corrected = _tag_illegible_with_bbox(corrected, all_disputes)
    return corrected, summary


def process_page(ark_id: str, page_num: int, total_pages: int,
                 unt_ocr_text: str, issue_meta: dict,
                 api_key: str, correction_prompt: str,
                 tess_lang: str | None,
                 abbyy_page_tokens: list,
                 abbyy_blocks: list,
                 expected_cols: int = 5,
                 worker_id: str = "",
                 rate_limiter=None) -> tuple:
    """
    Full pipeline for one page (backwards-compatible wrapper).
    Runs local stages then Claude stages in sequence.
    Returns (corrected_text, pipeline_summary).
    """
    local = process_page_local(
        ark_id, page_num, total_pages, unt_ocr_text, issue_meta,
        tess_lang, abbyy_page_tokens, abbyy_blocks,
        expected_cols, worker_id)
    return process_page_claude(
        local, ark_id, total_pages, issue_meta,
        api_key, correction_prompt, rate_limiter)


# ============================================================================
# POST-CORRECTION PROOFREADING (Stage 9)
# ============================================================================

PROOFREAD_PROMPT = """You are an expert proofreader for 19th-century German newspaper text \
recovered via OCR from Fraktur typeface on 35mm microfilm.

You receive one page of OCR-corrected text from an 1891 German-language Texas newspaper.

TASK: Fix spelling errors, obvious OCR artifacts, and broken words. Do NOT alter meaning.

RULES:
  • Fix common Fraktur OCR errors: ſ/s confusion, ff/ff, ä↔a, ü↔u, ö↔o transpositions
  • Rejoin broken compound words ("Bürger meister" → "Bürgermeister")
  • Rejoin hyphenated line breaks ("Zeitungs-\\nredakteur" → "Zeitungsredakteur")
  • Correct obvious misspellings while respecting period-appropriate German spelling
  • Preserve [unleserlich] and [unleserlich bbox=x,y,w,h] markers EXACTLY — never modify
  • Preserve [Column N] markers EXACTLY
  • Preserve paragraph breaks and overall text structure
  • Do NOT add, remove, or rearrange content
  • Do NOT translate — text stays in the original language
  • If a word looks unusual but could be valid 1890s German, a Texas place name,
    or a proper noun, keep it unchanged

OUTPUT: Return the corrected text only. No commentary, no markdown, no JSON wrapping."""


def proofread_page(page_num: int, corrected_text: str,
                   api_key: str, rate_limiter=None) -> str:
    """Stage 9: Claude proofreads corrected page text for spelling/OCR errors."""
    if not corrected_text.strip() or corrected_text.startswith("[CORRECTION FAILED"):
        return corrected_text

    result = claude_api_call(
        {"model": CLAUDE_MODEL, "max_tokens": 8000,
         "system": PROOFREAD_PROMPT,
         "messages": [{"role": "user", "content":
             f"PAGE {page_num}\n\n{corrected_text}"}]},
        api_key, rate_limiter, est_tokens=6000)

    if not result.strip():
        return corrected_text  # fallback to unproofread

    # Verify [unleserlich] markers were preserved — reject if any were lost
    orig_illegible = len(re.findall(r'\[unleserlich(?:\s+bbox=[^\]]+)?\]', corrected_text))
    new_illegible = len(re.findall(r'\[unleserlich(?:\s+bbox=[^\]]+)?\]', result))
    if orig_illegible > 0 and new_illegible < orig_illegible:
        tprint(f"    ⚠ p{page_num:02d} proofread dropped {orig_illegible - new_illegible} "
               f"[unleserlich] marker(s) — keeping original", level=2)
        return corrected_text

    return result


# ============================================================================
# ARTICLE SEGMENTATION (Stage 10a)
# ============================================================================

SEGMENTATION_PROMPT = f"""You are an expert in historical German newspaper structure.

You receive the corrected OCR text of ONE PAGE from an 1891 German-language
Texas newspaper. Text is in column-reading order with [Column N] markers.

TASK: Segment the page into discrete articles and advertisements.

RULES:
  • Each article, ad, notice, poem, or classified is a SEPARATE item
  • Headlines and datelines (e.g. "Berlin, 3. Sept.") mark article starts
  • Masthead (newspaper title/date/volume) = type "masthead"
  • Legal notices (Bekanntmachung, Aufruf) = type "notice"
  • Poetry or verse = type "poetry"
  • Wire-service items with datelines = type "article"
  • Commercial content = type "advertisement"
  • Default for news = type "article"
  • If an item clearly starts mid-sentence (no headline), set continues_from_prev: true
  • If an item clearly ends mid-sentence, set continues_to_next: true
  • Preserve {ILLEGIBLE} markers exactly

OUTPUT — valid JSON only, no markdown:
{{"page": <int>, "items": [{{"type": "article|advertisement|masthead|notice|poetry",
  "headline": "<first line or empty>", "body": "<full text>",
  "continues_from_prev": false, "continues_to_next": false}}]}}"""


def _local_segment_page(page_num: int, corrected_text: str) -> list | None:
    """
    Attempt local segmentation without Claude. Returns items list on success,
    None if the page is too complex for local heuristics.

    Local heuristics handle:
      - Page 1 masthead detection (title + date + volume at top)
      - Obvious headline breaks (ALL-CAPS or centered lines after whitespace)
      - Advertisement markers (common German ad patterns)

    Returns None (defer to Claude) when:
      - Multiple potential article boundaries are ambiguous
      - Page has complex multi-column structure with unclear breaks
    """
    if not corrected_text.strip():
        return []

    lines = corrected_text.split('\n')
    # Strip [Column N] markers for analysis but preserve structure
    content_lines = [l for l in lines if not l.strip().startswith('[Column ')]

    if not content_lines:
        return []

    # ── Page 1 masthead: first few lines are typically title/date/volume ──
    if page_num == 1 and len(content_lines) >= 3:
        # Check if first line looks like a newspaper title (short, possibly caps)
        first = content_lines[0].strip()
        if first and len(first) < 80:
            # Simple case: page 1 with a clear masthead then content
            # Find where masthead ends (blank line or dateline pattern)
            mast_end = 0
            for i, line in enumerate(content_lines[:8]):
                if not line.strip():
                    mast_end = i
                    break
                # Dateline patterns like "Bellville, den 17. September 1891"
                if re.search(r'\b\d{4}\b', line) and re.search(r'[A-Z][a-z]+ville|den \d', line):
                    mast_end = i + 1
                    break
            # Don't try to be too clever — only handle obvious single-body pages
            # Fall through to Claude for complex cases

    # ── Simple pages: if text has no clear article boundaries, treat as one ──
    # Look for headline indicators: blank line + short bold/caps line
    headline_indices = []
    for i, line in enumerate(content_lines):
        stripped = line.strip()
        if not stripped:
            continue
        # A headline candidate: preceded by blank line, short, possibly caps
        if i > 0 and not content_lines[i-1].strip() and len(stripped) < 60:
            # Check for dateline patterns (city + date)
            if re.search(r'^[A-ZÄÖÜ][a-zäöüß]+,\s+\d', stripped):
                headline_indices.append(i)
            # ALL CAPS or mostly caps lines
            elif len(stripped) > 3 and sum(1 for c in stripped if c.isupper()) > len(stripped) * 0.6:
                headline_indices.append(i)

    # If 0 or 1 headline found, it's a simple page — handle locally
    if len(headline_indices) <= 1:
        body = corrected_text.strip()
        if not body:
            return []
        return [{"type": "article", "headline": "", "body": body,
                 "continues_from_prev": False, "continues_to_next": False,
                 "page": page_num, "page_span": [page_num, page_num]}]

    # Multiple possible article breaks — too complex for local heuristics
    return None


def segment_page(page_num: int, corrected_text: str,
                 api_key: str, rate_limiter=None) -> list:
    """Segment corrected page text into discrete articles/ads."""
    if not corrected_text.strip():
        return []

    # Try local segmentation first to save API costs
    local_result = _local_segment_page(page_num, corrected_text)
    if local_result is not None:
        tprint(f"  │    p{page_num:02d} segmented locally ({len(local_result)} items)", level=3)
        return local_result

    # Fall back to Claude for complex pages
    tprint(f"  │    p{page_num:02d} too complex for local — sending to Claude", level=3)
    raw = claude_api_call(
        {"model": CLAUDE_MODEL, "max_tokens": 4000,
         "system": SEGMENTATION_PROMPT,
         "messages": [{"role": "user", "content":
             f"PAGE {page_num}\n\n{corrected_text}"}]},
        api_key, rate_limiter, est_tokens=3000)
    try:
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.M)
        data  = json.loads(clean)
        items = data.get("items", [])
        for item in items:
            item["page"]      = page_num
            item["page_span"] = [page_num, page_num]
        return items
    except Exception as e:
        tprint(f"    ⚠ Segmentation parse error p{page_num}: {e}", level=2)
        return [{"type": "article", "headline": "", "body": corrected_text,
                 "page": page_num, "page_span": [page_num, page_num],
                 "continues_from_prev": False, "continues_to_next": False}]


# ============================================================================
# PAGE-BOUNDARY STITCHING (Stage 10b)
# ============================================================================

STITCH_PROMPT = """You are an expert in historical German newspaper layout.

You receive the LAST ITEM from page N and the FIRST ITEM from page N+1.
Determine if they are the same article continued across the page break.

MERGE signals: last item ends mid-sentence, first item has no headline,
same topic/voice, either flagged continues_to/from.

SEPARATE signals: first item has a headline or dateline, clear topic change,
last item ends with period or closing.

OUTPUT — valid JSON only: {"decision": "merge"|"separate", "reason": "<one sentence>"}"""


def stitch_boundary(last_item: dict, first_item: dict,
                    api_key: str, rate_limiter=None) -> str:
    last_body  = last_item.get("body", "").strip()
    first_body = first_item.get("body", "").strip()
    first_hl   = first_item.get("headline", "").strip()

    if first_hl and len(first_hl) > 3:
        return "separate"
    if last_body and last_body[-1] in ".!?\"" and not last_item.get("continues_to_next"):
        return "separate"
    if last_item.get("continues_to_next") and first_item.get("continues_from_prev"):
        return "merge"

    prompt = (f"PAGE {last_item.get('page')} last item (type={last_item.get('type')}):\n"
              f"...{last_body[-600:]}\n\n"
              f"PAGE {first_item.get('page')} first item (type={first_item.get('type')}, "
              f"headline={first_hl!r}):\n{first_body[:600]}...")
    raw = claude_api_call(
        {"model": CLAUDE_MODEL, "max_tokens": 100,
         "system": STITCH_PROMPT,
         "messages": [{"role": "user", "content": prompt}]},
        api_key, rate_limiter, est_tokens=500)
    try:
        clean = re.sub(r"```(?:json)?|```", "", raw).strip()
        dec   = json.loads(clean).get("decision", "separate")
        return dec if dec in ("merge", "separate") else "separate"
    except Exception:
        return "separate"


def stitch_all_pages(all_items: list, api_key: str,
                     rate_limiter=None, worker_id: str = "") -> list:
    if not all_items:
        return all_items
    pages = {}
    for item in all_items:
        pages.setdefault(item["page"], []).append(item)
    sorted_pgs = sorted(pages.keys())
    merges = 0
    for i in range(len(sorted_pgs) - 1):
        pa, pb = sorted_pgs[i], sorted_pgs[i+1]
        if not pages[pa] or not pages[pb]:
            continue
        last_item  = pages[pa][-1]
        first_item = pages[pb][0]
        if last_item.get("type") in ("masthead", "advertisement"):
            continue
        tprint(f"    stitch p{pa}→p{pb} ...", worker=worker_id, level=3)
        decision = stitch_boundary(last_item, first_item, api_key, rate_limiter)
        if decision == "merge":
            merged_body = last_item["body"].rstrip() + "\n\n" + first_item["body"].lstrip()
            last_item["body"]      = merged_body
            last_item["page_span"] = [last_item["page_span"][0],
                                       max(last_item["page_span"][-1],
                                           first_item["page_span"][-1])]
            last_item["continues_to_next"] = first_item.get("continues_to_next", False)
            pages[pb].pop(0)
            merges += 1
            tprint(f"    ✓ merged p{pa}→p{pb}", worker=worker_id, level=3)
    if merges:
        tprint(f"  Stitched {merges} cross-page article(s)", worker=worker_id, level=1)
    result = []
    for pg in sorted_pgs:
        result.extend(pages.get(pg, []))
    return result


# ============================================================================
# ARTICLE FILE WRITING
# ============================================================================

def write_article_files(issue: dict, all_items: list, ark_dir: Path) -> int:
    """
    Write one .txt file per article/ad. Also writes manifest.json.

    Filename: {ark_id}_{date}_art{NNN}.txt
    Example:  metapth1478562_1891-09-17_art003.txt

    File format:
      ARK:     {ark_id}
      ISSUE:   {full_title}
      PAGE:    {first_page}  (of {total})
      SPANS:   {first}-{last}     ← only when cross-page
      TYPE:    article|advertisement|masthead|notice|poetry

      {headline}

      {body with [unleserlich] preserved exactly}
    """
    ark_dir.mkdir(parents=True, exist_ok=True)
    # Clean old article files (both legacy pg*_art* and new naming patterns)
    for old in ark_dir.glob("pg*_art*.txt"):
        old.unlink()
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
        spans    = f"{pg_first}-{pg_last}" if pg_first != pg_last else str(pg_first)
        fname    = f"{ark_id}_{issue_date}_art{art_num:03d}.txt"

        lines = [
            f"ARK:     {ark_id}",
            f"ISSUE:   {full_title}",
            f"PAGE:    {pg_first}  (of {total_pgs})",
        ]
        if pg_first != pg_last:
            lines.append(f"SPANS:   {spans}")
        lines.append(f"TYPE:    {item.get('type','article')}")
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
# IMAGE FETCHING
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
                    ct, data = resp.headers.get("Content-Type",""), resp.read()
                    if len(data) >= 50_000 and ("image" in ct or url.endswith(".jpg")):
                        return data
            except urllib.error.HTTPError as e:
                if e.code == 429: time.sleep(30*attempt)
            except Exception:
                continue
        if attempt < max_retries: time.sleep(10*attempt)
    return None

def is_valid_cached_image(path: Path) -> bool:
    return path.exists() and path.stat().st_size >= 50_000

def fetch_page_image(ark_id: str, page: int) -> tuple:
    p = local_image_path(ark_id, page)
    if is_valid_cached_image(p):
        return p.read_bytes(), "image/jpeg"
    if p.exists(): p.unlink()
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
    """Strip UNT portal HTML from raw OCR download, return plain text."""
    if '<' not in text: return text
    m = re.search(r'id=["\']ocr-text["\'][^>]*>(.*?)</(?:div|section)',
                  text, re.S | re.I)
    inner = m.group(1) if m else text
    inner = re.sub(r'<br\s*/?>', '\n', inner, flags=re.I)
    inner = re.sub(r'<[^>]{0,500}>', ' ', inner)
    for e, r in [('&amp;','&'),('&lt;',''),('&gt;',''),
                 ('&quot;','"'),('&nbsp;',' '),('&#x27;',"'")]:
        inner = inner.replace(e, r)
    inner = re.sub(r'&#[xX][0-9a-fA-F]{1,6};', '', inner)
    inner = re.sub(r'&#\d{1,6};', '', inner)
    inner = re.sub(r'[ \t]{2,}', ' ', inner)
    inner = re.sub(r'\n{3,}', '\n\n', inner)
    return inner.strip()

def parse_ocr_pages(text: str) -> tuple:
    lines = text.replace('\r\n','\n').replace('\r','\n').split('\n')
    hlines, blines, in_h = [], [], True
    for line in lines:
        if in_h:
            hlines.append(line)
            if line.startswith('='*10): in_h = False
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
# ISSUE PROCESSING
# ============================================================================

def process_issue(issue, api_key, correction_prompt, delay,
                  resume, retry_failed, tess_lang,
                  rate_limiter=None, worker_id=""):
    ark_id = issue["ark_id"]
    vol    = str(issue.get("volume","?")).zfill(2)
    num    = str(issue.get("number","?")).zfill(2)
    date   = re.sub(r"[^\w\-]", "-", issue.get("date","unknown"))
    fname  = f"{ark_id}_vol{vol}_no{num}_{date}.txt"

    ocr_path  = OCR_DIR       / fname
    corr_path = CORRECTED_DIR / fname
    ark_dir   = ARTICLES_DIR  / ark_id

    if not ocr_path.exists():
        tprint(f"  ⚠ OCR not found: {fname}", worker=worker_id, level=1)
        return "missing_ocr"

    if resume and not retry_failed and corr_path.exists() and corr_path.stat().st_size > 500:
        if ark_dir.exists() and any(ark_dir.glob("*_art*.txt")):
            tprint(f"  SKIP {fname}", worker=worker_id, level=1)
            return "skipped"

    ocr_raw = ocr_path.read_text(encoding="utf-8", errors="replace")
    header, ocr_pages = parse_ocr_pages(ocr_raw)
    if not ocr_pages:
        tprint(f"  ⚠ Could not parse pages in {fname}", worker=worker_id, level=1)
        return "parse_error"

    actual_pages = max(ocr_pages.keys())

    existing_corrected = {}
    if (resume or retry_failed) and corr_path.exists():
        _, existing_corrected = parse_ocr_pages(
            corr_path.read_text(encoding="utf-8", errors="replace"))

    pages_to_process = []
    for pg in range(1, actual_pages + 1):
        existing = existing_corrected.get(pg, "")
        keep = retry_failed and existing and not existing.startswith("[CORRECTION FAILED")
        pages_to_process.append((pg, not keep))

    redo = sum(1 for _, r in pages_to_process if r)

    # Check for ABBYY XML (one XML covers whole issue, multiple pages inside)
    axml = abbyy_xml_path(fname)
    has_abbyy = axml is not None

    # ── Stage 0: Layout analysis ──────────────────────────────────────────
    # Check for calibration data first; if none, run auto-detection and save.
    collection_dir = CORRECTED_DIR.parent
    cal = load_calibration(collection_dir)
    pages_to_analyze = [pg for pg, needs_redo in pages_to_process if needs_redo]

    if cal and cal.get("page_layouts"):
        # Use calibrated layout data
        expected_cols = cal.get("expected_cols", 5)
        page_layouts = {}
        for pg in pages_to_analyze:
            pg_str = str(pg)
            if pg_str in cal["page_layouts"]:
                bounds = detect_content_bounds(
                    cv2.imdecode(
                        np.frombuffer(fetch_page_image(ark_id, pg)[0], np.uint8),
                        cv2.IMREAD_GRAYSCALE))
                page_layouts[pg] = _layout_from_calibration(
                    cal["page_layouts"][pg_str], pg, bounds,
                    skew_deg=cal.get("skew_deg", 0.0))
            # Pages not in calibration will get auto-detected in process_page_local
        tprint(f"  Using calibration: {expected_cols} cols, "
               f"{len(page_layouts)} page layouts loaded",
               worker=worker_id, level=1)
    else:
        # Auto-detect and save calibration for user review
        expected_cols, page_layouts = analyze_issue_layout(
            ark_id, pages_to_analyze, worker_id=worker_id)
        save_calibration(collection_dir, ark_id, expected_cols, page_layouts)

    engines = ["Tesseract" if tess_lang else None,
               "Kraken" if HAS_KRAKEN else None,
               "ABBYY" if has_abbyy else None]
    engine_str = "+".join(e for e in engines if e) or "Claude-only"
    tprint(f"  {actual_pages}pp  redo={redo}  engines={engine_str}", worker=worker_id, level=1)
    if has_abbyy:
        tprint(f"  ABBYY XML: {axml.name} — treating as one source among several",
               worker=worker_id, level=1)

    corrected_pages = dict(existing_corrected)

    # ══════════════════════════════════════════════════════════════════════
    # PASS 1 — HIGH CONFIDENCE: Local OCR engines (stages 1-7)
    # Tesseract + Kraken + ABBYY → word alignment → agreed/disputed split
    # No API calls. Produces high-confidence agreed text and dispute table.
    # ══════════════════════════════════════════════════════════════════════
    tprint(f"  ┌─ PASS 1: Local OCR (high-confidence extraction) ─────────",
           worker=worker_id, level=1)
    local_results = {}
    for pg, needs_redo in pages_to_process:
        if not needs_redo:
            tprint(f"  │  p{pg:02d} KEEP (already corrected)", worker=worker_id, level=2)
            continue

        unt_ocr = strip_ocr_html(ocr_pages.get(pg, ""))
        tprint(f"  │  p{pg:02d}/{actual_pages} running local OCR engines ...",
               worker=worker_id, level=1)

        abbyy_tokens, abbyy_blocks = [], []
        if has_abbyy:
            tprint(f"  │    parsing ABBYY XML page {pg} ...", worker=worker_id, level=3)
            abbyy_tokens, abbyy_blocks = parse_abbyy_page(axml, page_index=pg - 1)
            tprint(f"  │    → {len(abbyy_tokens)} ABBYY tokens, "
                   f"{len(abbyy_blocks)} blocks", worker=worker_id, level=3)

        try:
            local = process_page_local(
                ark_id, pg, actual_pages, unt_ocr, issue,
                tess_lang=tess_lang,
                abbyy_page_tokens=abbyy_tokens,
                abbyy_blocks=abbyy_blocks,
                expected_cols=expected_cols,
                worker_id=worker_id,
                page_layout=page_layouts.get(pg),
            )
            local_results[pg] = local
            disputes = len(local.get("all_disputes", []))
            tprint(f"  │  p{pg:02d} ✓ [{local['summary']}]",
                   worker=worker_id, level=1)
        except Exception as e:
            tprint(f"  │  p{pg:02d} ✗ FAILED: {e}", worker=worker_id, level=1)
            corrected_pages[pg] = f"[CORRECTION FAILED: {e}]\n\n{unt_ocr}"

    # Summary of disputes across all pages
    total_disputes = sum(len(r.get("all_disputes", []))
                         for r in local_results.values())
    total_agreed = sum(
        sum(1 for l in r.get("agreed_text", "").split("\n")
            if l.strip() and not l.startswith("["))
        for r in local_results.values())
    pages_needing_claude = sum(1 for r in local_results.values()
                               if len(r.get("all_disputes", [])) > 0
                               or r.get("no_image"))
    tprint(f"  └─ PASS 1 complete: {len(local_results)} pages, "
           f"~{total_agreed} agreed words, {total_disputes} disputes",
           worker=worker_id, level=1)

    # ══════════════════════════════════════════════════════════════════════
    # PASS 2 — LOW CONFIDENCE: Claude arbitration (stage 8) + proofread (9)
    # Sends disputed words + page image to Claude for resolution.
    # Then proofreads each corrected page for residual errors.
    # ══════════════════════════════════════════════════════════════════════
    tprint(f"  ┌─ PASS 2: Claude arbitration (low-confidence resolution) ─",
           worker=worker_id, level=1)
    tprint(f"  │  {pages_needing_claude} page(s) have disputes to resolve",
           worker=worker_id, level=1)

    for pg in sorted(local_results.keys()):
        local = local_results[pg]
        n_disputes = len(local.get("all_disputes", []))
        try:
            corrected, summary = process_page_claude(
                local, ark_id, actual_pages, issue,
                api_key, correction_prompt, rate_limiter)
            corrected_pages[pg] = corrected
            snippet = corrected[:60].replace("\n", " ")
            tprint(f"  │  p{pg:02d} ✓ \"{snippet}...\"",
                   worker=worker_id, level=2)
        except Exception as e:
            tprint(f"  │  p{pg:02d} ✗ FAILED: {e}", worker=worker_id, level=1)
            corrected_pages[pg] = (f"[CORRECTION FAILED: {e}]\n\n"
                                   + local.get("agreed_text", ""))
        time.sleep(delay)

    tprint(f"  └─ PASS 2 arbitration complete", worker=worker_id, level=1)

    # ── Stage 9: Proofreading pass ────────────────────────────────────────
    tprint(f"  ┌─ Stage 9: Proofreading ──────────────────────────────────",
           worker=worker_id, level=1)
    proofread_count = 0
    for pg in sorted(corrected_pages.keys()):
        text = corrected_pages[pg]
        if text.startswith("[CORRECTION FAILED"):
            continue
        tprint(f"  │  p{pg:02d} proofreading ...", worker=worker_id, level=2)
        proofread = proofread_page(pg, text, api_key, rate_limiter)
        if proofread != text:
            corrected_pages[pg] = proofread
            proofread_count += 1
            tprint(f"  │  p{pg:02d} ✓ revised", worker=worker_id, level=2)
        else:
            tprint(f"  │  p{pg:02d} ✓ no changes", worker=worker_id, level=2)
        time.sleep(delay)
    tprint(f"  └─ Proofread: {proofread_count}/{len(corrected_pages)} page(s) revised",
           worker=worker_id, level=1)

    # Write corrected/ file (used by translate step)
    out_lines = [header, ""]
    for pg in sorted(corrected_pages.keys()):
        out_lines.append(f"--- Page {pg} of {actual_pages} ---")
        out_lines.append(corrected_pages[pg])
        out_lines.append("")
    corr_path.write_text('\n'.join(out_lines), encoding="utf-8")
    tprint(f"  → corrected/{fname}  ({corr_path.stat().st_size//1024}KB)",
           worker=worker_id, level=1)

    # ── Stage 10a: Article segmentation ─────────────────────────────────────
    tprint(f"  ┌─ Stage 10: Article segmentation + stitching ─────────────",
           worker=worker_id, level=1)
    all_items = []
    for pg in sorted(corrected_pages.keys()):
        text = corrected_pages[pg]
        if text.startswith("[CORRECTION FAILED"):
            continue
        tprint(f"  │  p{pg:02d} segmenting ...", worker=worker_id, level=2)
        items = segment_page(pg, text, api_key, rate_limiter)
        all_items.extend(items)
        types = {}
        for it in items:
            types[it.get("type", "?")] = types.get(it.get("type", "?"), 0) + 1
        type_str = ", ".join(f"{v} {k}" for k, v in types.items())
        tprint(f"  │  p{pg:02d} → {len(items)} item(s): {type_str}",
               worker=worker_id, level=1)
        if LOG_LEVEL >= 5:
            for it in items:
                hl = it.get("headline", "")[:60] or "(no headline)"
                tprint(f"  │    {it.get('type','?')}: {hl}", level=5)
        time.sleep(delay)

    # Stage 10b: Cross-page stitching
    if len(corrected_pages) > 1 and all_items:
        tprint(f"  │  stitching across page boundaries ...", worker=worker_id, level=1)
        all_items = stitch_all_pages(all_items, api_key, rate_limiter, worker_id)

    # Stage 11: Write article files
    n = write_article_files(issue, all_items, ark_dir)
    tprint(f"  └─ → articles/{ark_id}/  ({n} files)", worker=worker_id, level=1)
    return "ok"


# ============================================================================
# IMAGE PRELOAD
# ============================================================================

PRELOAD_LOG_NAME = "preload_failures.json"

def load_preload_log() -> dict:
    p = IMAGES_DIR / PRELOAD_LOG_NAME
    try: return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception: return {}

def save_preload_log(failures: dict):
    (IMAGES_DIR / PRELOAD_LOG_NAME).write_text(
        json.dumps(failures, indent=2), encoding="utf-8")

def _dl_one(task: dict) -> dict:
    if task["skip"]: return {**task, "status": task.get("skip_status","skipped")}
    ip = task["img_path"]
    if ip.exists() and not is_valid_cached_image(ip):
        try: ip.unlink()
        except Exception: pass
    data = download_image_from_unt(task["ark_id"], task["page"], max_retries=3)
    if data:
        try: ip.parent.mkdir(parents=True,exist_ok=True); ip.write_bytes(data)
        except Exception as e: return {**task,"status":"failed","error":str(e)}
        return {**task,"status":"ok","kb":len(data)//1024}
    return {**task,"status":"failed","error":"all URLs returned stubs"}

def preload_images(issues, resume=True, retry_failed=False, workers=4):
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    failures, fl = load_preload_log(), threading.Lock()
    ctr, cl = {"downloaded":0,"skipped":0,"failed":0}, threading.Lock()
    for issue in issues: (IMAGES_DIR/issue["ark_id"]).mkdir(exist_ok=True)
    total = sum(int(i.get("pages",8)) for i in issues)
    valid = sum(1 for i in issues for pg in range(1,int(i.get("pages",8))+1)
                if is_valid_cached_image(local_image_path(i["ark_id"],pg)))
    print(f"Preload: {total} pages, {valid} cached", flush=True)
    tasks = []
    for issue in issues:
        for pg in range(1, int(issue.get("pages",8))+1):
            path = local_image_path(issue["ark_id"],pg)
            fk   = f"{issue['ark_id']}/page_{pg:02d}"
            skip = is_valid_cached_image(path) or (not retry_failed and fk in failures)
            tasks.append({"ark_id":issue["ark_id"],"page":pg,"img_path":path,
                          "fail_key":fk,"skip":skip,
                          "skip_status":"skipped" if is_valid_cached_image(path) else "skip_fail",
                          "vol":issue.get("volume","?"),"num":issue.get("number","?")})
    announced = set()
    def handle(r):
        if r["ark_id"] not in announced:
            announced.add(r["ark_id"])
            print(f"  {r['ark_id']}  Vol.{r['vol']} No.{r['num']}", flush=True)
        s = r["status"]
        if s == "ok":
            print(f"    p{r['page']:02d} ✓  {r.get('kb',0)}KB", flush=True)
            with cl: ctr["downloaded"] += 1
            with fl:
                if r["fail_key"] in failures: del failures[r["fail_key"]]; save_preload_log(failures)
        elif s == "failed":
            print(f"    p{r['page']:02d} ✗  {r.get('error','')}", flush=True)
            with cl: ctr["failed"] += 1
            with fl:
                failures[r["fail_key"]] = {"ark_id":r["ark_id"],"page":r["page"],
                    "attempts":failures.get(r["fail_key"],{}).get("attempts",0)+1}
                save_preload_log(failures)
        else:
            with cl: ctr["skipped"] += 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_dl_one,t): t for t in tasks}
        for fut in as_completed(futs):
            try: handle(fut.result())
            except Exception as e: handle({**futs[fut],"status":"failed","error":str(e)})
    print(f"\nPreload: {ctr['downloaded']} downloaded, "
          f"{ctr['skipped']} skipped, {ctr['failed']} failed", flush=True)


# ============================================================================
# DEPENDENCY REPORT
# ============================================================================

def check_deps() -> str:
    lines = []
    lines.append(f"  OpenCV    : {'✓' if HAS_CV2 else '✗  pip install opencv-python-headless scipy'}")
    if HAS_TESSERACT:
        tess_cmd = getattr(pytesseract.pytesseract, 'tesseract_cmd', 'tesseract')
        lang = detect_tesseract_lang()
        if lang:
            lines.append(f"  Tesseract : ✓  lang={lang}  ({tess_cmd})")
        else:
            lines.append("  Tesseract : ✓ installed but NO Fraktur model")
            if sys.platform == "win32":
                lines.append("              Download deu.traineddata → Tesseract-OCR/tessdata/")
            else:
                lines.append("              apt-get install tesseract-ocr-deu")
    else:
        if sys.platform == "win32":
            lines.append("  Tesseract : ✗  Install from https://github.com/UB-Mannheim/tesseract/wiki")
        else:
            lines.append("  Tesseract : ✗  apt-get install tesseract-ocr")
    lines.append(f"  Kraken    : {'✓' if HAS_KRAKEN else '✗  pip install kraken  (optional)'}")
    lines.append(f"  LayoutParser: {'✓' if HAS_LAYOUTPARSER else '✗  pip install layoutparser  (optional, GPU recommended)'}")
    n_abbyy = len(list(ABBYY_DIR.glob("*.xml"))) if ABBYY_DIR and ABBYY_DIR.exists() else 0
    lines.append(f"  ABBYY XML : {n_abbyy} file(s) in abbyy/  "
                 f"{'(will use as one source)' if n_abbyy else '(optional — contact ana.krahmer@unt.edu)'}")
    return "\n".join(lines)


# ============================================================================
# MAIN
# ============================================================================

def main():
    p = argparse.ArgumentParser(
        description="UNT Archive — Multi-Engine OCR + Article Segmentation")
    p.add_argument("--config-path",    required=True)
    p.add_argument("--api-key",        default=os.environ.get("ANTHROPIC_API_KEY",""))
    p.add_argument("--preload-images", action="store_true")
    p.add_argument("--workers",        type=int, default=4)
    p.add_argument("--resume",         action="store_true")
    p.add_argument("--retry-failed",   action="store_true")
    p.add_argument("--ark",            default=None)
    p.add_argument("--date-from",      default=None)
    p.add_argument("--date-to",        default=None)
    p.add_argument("--delay",          type=float, default=2.0)
    p.add_argument("--api-workers",    type=int,   default=3)
    p.add_argument("--serial",         action="store_true")
    p.add_argument("--tier",           default="default",
                   choices=["default","build","custom"])
    p.add_argument("--logging",        type=int, default=1, choices=[1,2,3,4,5],
                   help="Log verbosity: 1=progress 2=pages 3=engines 4=alignment 5=verbose")
    p.add_argument("--verbose",        action="store_true",
                   help="Shorthand for --logging 5")
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

    # Load global config for API key and model defaults
    global_config_path = Path(__file__).parent / "config.json"
    global_config = {}
    if global_config_path.exists():
        try:
            global_config = json.loads(global_config_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    global CLAUDE_MODEL
    # Model priority: collection.json → config.json → default
    if config.get("claude_model"):
        CLAUDE_MODEL = config["claude_model"]
    elif global_config.get("claude_model"):
        CLAUDE_MODEL = global_config["claude_model"]

    # API key priority: flag → env → config.json → collection.json (legacy)
    api_key = (args.api_key
               or os.environ.get("ANTHROPIC_API_KEY", "")
               or global_config.get("anthropic_api_key", "")
               or config.get("anthropic_api_key", ""))
    if not args.preload_images and not api_key:
        sys.exit("Error: API key required. Set in config.json, ANTHROPIC_API_KEY env var, or --api-key flag.")

    index_path = METADATA_DIR / "all_issues.json"
    if not index_path.exists():
        sys.exit(f"No issue index at {index_path}. Run --discover first.")
    with open(index_path, encoding="utf-8") as f:
        all_issues = json.load(f)

    issues = all_issues
    if args.ark:       issues = [i for i in issues if i["ark_id"] == args.ark]
    if args.date_from: issues = [i for i in issues if i.get("date","") >= args.date_from]
    if args.date_to:   issues = [i for i in issues if i.get("date","") <= args.date_to]

    tess_lang = detect_tesseract_lang()

    print(f"Collection : {config['title_name']}", flush=True)
    print(f"Issues     : {len(issues)}", flush=True)
    print(f"Model      : {CLAUDE_MODEL}", flush=True)
    print(check_deps(), flush=True)

    cached   = sum(1 for i in issues for pg in range(1, int(i.get("pages",8))+1)
                   if is_valid_cached_image(local_image_path(i["ark_id"],pg)))
    total_pg = sum(int(i.get("pages",8)) for i in issues)
    print(f"Images     : {cached}/{total_pg} {'✓' if cached==total_pg else '(run --preload-images)'}",
          flush=True)

    if args.preload_images:
        preload_images(issues, resume=True, retry_failed=args.retry_failed,
                       workers=args.workers)
        return  # preload is a standalone step; user runs --correct separately

    correction_prompt = build_correction_prompt(config)
    rate_limiter = limiter_from_tier(args.tier) if ClaudeRateLimiter else None
    effective_workers = args.api_workers if not args.serial else 1

    log = []; log_lock = threading.Lock()
    ctr = {"ok":0,"skipped":0,"err":0}; cl = threading.Lock()
    CORRECTED_DIR.mkdir(parents=True, exist_ok=True)
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)

    def run_issue(idx_issue):
        idx, issue = idx_issue
        ark_id = issue["ark_id"]
        wid    = ""
        tprint(f"\n{'─'*60}", level=1)
        tprint(f"[{idx+1}/{len(issues)}] {ark_id}  "
               f"Vol.{issue.get('volume','?')} No.{issue.get('number','?')}  "
               f"{issue.get('date','')}", level=1)
        status = process_issue(
            issue, api_key, correction_prompt,
            args.delay, args.resume, args.retry_failed,
            tess_lang=tess_lang,
            rate_limiter=rate_limiter, worker_id=wid)
        with log_lock:
            log.append({"ark_id": ark_id, "status": status})
            (CORRECTED_DIR/"correction_log.json").write_text(
                json.dumps(log, indent=2), encoding="utf-8")
        with cl:
            if   status=="ok":      ctr["ok"]      += 1
            elif status=="skipped": ctr["skipped"] += 1
            else:                   ctr["err"]     += 1
        return status

    # ── Process all issues sequentially ─────────────────────────────────
    # Local OCR is CPU/GPU-bound — parallelizing just interleaves output
    # and thrashes the same hardware. Issues run one at a time; the Claude
    # API phase inside each issue can still use rate_limiter concurrency.
    all_items = list(enumerate(issues))
    for item in all_items:
        run_issue(item)

    if rate_limiter: tprint(f"\nRate limiter: {rate_limiter.status_line()}", level=1)
    tprint(f"\n{'='*50}", level=1)
    tprint(f"Complete: {ctr['ok']}  Skipped: {ctr['skipped']}  Errors: {ctr['err']}", level=1)
    tprint(f"Corrected: {CORRECTED_DIR}", level=1)
    tprint(f"Articles:  {ARTICLES_DIR}", level=1)

if __name__ == "__main__":
    main()
