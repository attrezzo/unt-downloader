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

# The single canonical unintelligible marker — used by every source
ILLEGIBLE = "[unleserlich]"

sys.stdout.reconfigure(line_buffering=True)
_print_lock = threading.Lock()
def _sanitize_date(date_str: str) -> str:
    """Replace non-word/non-hyphen chars in a date string for use in filenames."""
    return re.sub(r'[^\w-]', '-', date_str)

def tprint(*args, worker: str = "", **kwargs):
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
        tprint(f"  ⚠ ABBYY parse error ({xml_path.name} page {page_index}): {e}")
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
    """Trim dark microfilm borders. Returns (left, top, right, bottom)."""
    if not HAS_CV2:
        h, w = img_gray.shape
        return int(w*.05), int(h*.02), int(w*.97), int(h*.98)
    h, w  = img_gray.shape
    _, bw = cv2.threshold(img_gray, 60, 255, cv2.THRESH_BINARY)
    cl    = np.mean(bw, axis=0)
    rl    = np.mean(bw, axis=1)
    left  = int(np.argmax(cl > 120)) + 5
    right = int(w - np.argmax((cl > 120)[::-1])) - 5
    top   = int(np.argmax(rl > 120)) + 5
    bot   = int(h - np.argmax((rl > 120)[::-1])) - 5
    return max(0,left), max(0,top), min(w,right), min(h,bot)


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
        return tokens
    except Exception as e:
        tprint(f"    ⚠ Tesseract ({source_tag}) error: {e}")
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

    tprint(f"  Downloading Kraken model → {dest}")
    tprint(f"    URL: {_KRAKEN_MODEL_URL.split('?')[0]}")
    req = urllib.request.Request(_KRAKEN_MODEL_URL,
                                headers={"User-Agent": "UNT-Archive/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    dest.write_bytes(data)
    tprint(f"    ✓ {len(data) // 1024:,} KB downloaded")
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
                # Re-check after acquiring lock (another thread may have loaded)
                if _KRAKEN_MODEL is None:
                    model_path = _find_kraken_model()
                    if not model_path:
                        model_path = _download_kraken_model()
                    _KRAKEN_MODEL = kraken_models.load_any(model_path)
                    tprint(f"  Kraken model: {model_path}")
        pil = PILImage.fromarray(col_img)
        # Suppress Kraken's noisy polygonizer warnings on degraded scans.
        # These come from both the logging system and direct stderr writes.
        import warnings, logging, io
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            _kraken_logger = logging.getLogger("kraken")
            old_level = _kraken_logger.level
            _kraken_logger.setLevel(logging.CRITICAL)
            old_stderr = sys.stderr
            try:
                sys.stderr = io.StringIO()  # swallow polygonizer spam
                seg = blla.segment(pil)
            finally:
                sys.stderr = old_stderr
                _kraken_logger.setLevel(old_level)
        tokens = []
        for record in rpred.rpred(_KRAKEN_MODEL, pil, seg):
            text = record.prediction.strip()
            conf = int(record.confidences[0] * 100) if record.confidences else 50
            if not text:
                continue
            bbox = record.bbox
            tokens.append({
                "text":   ILLEGIBLE if conf < 10 else text,
                "conf":   conf,
                "source": source_tag,
                "left": bbox[0], "top": bbox[1],
                "right": bbox[2], "bottom": bbox[3],
            })
        return tokens
    except Exception as e:
        # Disable Kraken for the rest of the run to avoid per-column spam
        err = str(e).lower()
        if "model" in err or "not loadable" in err or ".mlmodel" in err:
            HAS_KRAKEN = False
            tprint(f"  ⚠ Kraken disabled for this run: {e}")
        else:
            tprint(f"    ⚠ Kraken error: {e}")
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
            if rate_limiter:
                usage = result.get("usage", {})
                rate_limiter.record_usage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0))
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
                       worker_id: str = "") -> dict:
    """
    LOCAL stages only (1-7): image → preprocess → columns → Tesseract → Kraken → align.
    Returns a dict with all data needed for Claude (or a no-disputes fast path).

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
        tprint(f"    p{page_num:02d} ⚠ image unavailable", worker=worker_id)
        result["no_image"] = True
        result["agreed_text"] = unt_ocr_text[:3000]
        result["all_disputes"] = []
        result["img_bytes"] = None
        result["summary"] = "no-image"
        return result

    result["img_bytes"] = img_bytes
    nparr    = np.frombuffer(img_bytes, np.uint8)
    img_gray = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
    enhanced = preprocess_image(img_gray)

    # ── Detect columns from image (always) ───────────────────────────────
    bounds       = detect_content_bounds(img_gray)
    left, top, right, bot = bounds
    opencv_cols  = detect_columns_from_image(img_gray, bounds, expected_cols=expected_cols)
    n_opencv     = len(opencv_cols)

    # ── Boundary comparison (if ABBYY data present) ───────────────────────
    final_cols = opencv_cols
    if abbyy_blocks:
        abbyy_gutters = abbyy_column_boundaries(abbyy_blocks)
        final_cols, boundary_report = compare_boundaries(
            opencv_cols, abbyy_gutters, expected_cols=expected_cols)
        if boundary_report and boundary_report != "Boundaries agree":
            tprint(f"    p{page_num:02d} boundary comparison:\n{boundary_report}",
                   worker=worker_id)

    # ── Assign ABBYY tokens to columns ────────────────────────────────────
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

    # ── OCR each column (Tesseract + Kraken) ──────────────────────────────
    all_agreed_lines = []
    all_disputes     = []
    engines_used     = set()

    for col_idx, (cx1, cx2) in enumerate(final_cols, 1):
        strip = enhanced[top:bot, cx1:cx2]
        if strip.shape[1] < 50:
            continue

        sources: dict[str, list] = {}

        if col_idx in abbyy_by_col:
            sources["abbyy"] = abbyy_by_col[col_idx]
            engines_used.add("abbyy")

        if HAS_TESSERACT and tess_lang:
            ta = tesseract_tokens(strip, tess_lang, TESS_PSM_A, "tess_a")
            tb = tesseract_tokens(strip, tess_lang, TESS_PSM_B, "tess_b")
            if ta: sources["tess_a"] = ta; engines_used.add("tess_a")
            if tb: sources["tess_b"] = tb; engines_used.add("tess_b")

        if HAS_KRAKEN:
            kr = kraken_tokens(strip)
            if kr: sources["kraken"] = kr; engines_used.add("kraken")

        if not sources:
            continue

        aligned           = align_sources(sources)
        agreed_lines, dis = split_agree_dispute(aligned)

        for d in dis:
            d["page_left"]   = d["left"]   + cx1
            d["page_top"]    = d["top"]    + top
            d["page_right"]  = d.get("right",  d["left"] + 30) + cx1
            d["page_bottom"] = d.get("bottom", d["top"]  + 20) + top
            d["column"]      = col_idx

        all_agreed_lines.append(f"[Column {col_idx}]")
        all_agreed_lines.extend(agreed_lines)
        all_disputes.extend(dis)

    agreed_text = "\n".join(all_agreed_lines)
    if not agreed_text.strip():
        agreed_text = unt_ocr_text[:3000]

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
        raw = claude_api_call(
            {"model": CLAUDE_MODEL, "max_tokens": 6000,
             "system": correction_prompt,
             "messages": [{"role": "user", "content":
                 f"PAGE {page_num}: Correct this OCR text (no image available):\n{local_result['unt_ocr_text']}"}]},
            api_key, rate_limiter, est_tokens=3000)
        return raw, "no-image"

    # No disputes — all engines agree, skip Claude
    if len(all_disputes) == 0 and agreed_text.strip():
        corrected = re.sub(r'\{\?([^?}]*)\?\}', r'\1', agreed_text)
        corrected = _tag_illegible_with_bbox(corrected, all_disputes)
        summary += "  claude=skipped(no-disputes)"
        return corrected, summary

    # Stage 8: Claude arbitrates disputes
    corrected = arbitrate_with_claude(
        ark_id, page_num, total_pages,
        agreed_text, all_disputes,
        issue_meta, api_key, correction_prompt,
        rate_limiter=rate_limiter)

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
               f"[unleserlich] marker(s) — keeping original")
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
        return local_result

    # Fall back to Claude for complex pages
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
        tprint(f"    ⚠ Segmentation parse error p{page_num}: {e}")
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
        tprint(f"    stitch p{pa}→p{pb} ...", worker=worker_id)
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
            tprint(f"    ✓ merged p{pa}→p{pb}", worker=worker_id)
    if merges:
        tprint(f"  Stitched {merges} cross-page article(s)", worker=worker_id)
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
        tprint(f"  ⚠ OCR not found: {fname}", worker=worker_id)
        return "missing_ocr"

    if resume and not retry_failed and corr_path.exists() and corr_path.stat().st_size > 500:
        if ark_dir.exists() and any(ark_dir.glob("*_art*.txt")):
            tprint(f"  SKIP {fname}", worker=worker_id)
            return "skipped"

    ocr_raw = ocr_path.read_text(encoding="utf-8", errors="replace")
    header, ocr_pages = parse_ocr_pages(ocr_raw)
    if not ocr_pages:
        tprint(f"  ⚠ Could not parse pages in {fname}", worker=worker_id)
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
    expected_cols = int(issue_meta_config.get("expected_cols", 5)) if hasattr(process_issue, '_config') else 5
    engines = ["Tesseract" if tess_lang else None,
               "Kraken" if HAS_KRAKEN else None,
               "ABBYY" if has_abbyy else None]
    engine_str = "+".join(e for e in engines if e) or "Claude-only"
    tprint(f"  {actual_pages}pp  redo={redo}  engines={engine_str}", worker=worker_id)
    if has_abbyy:
        tprint(f"  ABBYY XML: {axml.name} — treating as one source among several",
               worker=worker_id)

    corrected_pages = dict(existing_corrected)

    # ── Phase A: LOCAL stages (1-7) for all pages ─────────────────────────
    # Runs Tesseract, Kraken, ABBYY parsing, alignment — no API calls.
    local_results = {}
    for pg, needs_redo in pages_to_process:
        if not needs_redo:
            tprint(f"    p{pg:02d} KEEP", worker=worker_id)
            continue

        unt_ocr = strip_ocr_html(ocr_pages.get(pg, ""))
        tprint(f"    p{pg:02d}/{actual_pages} local OCR ...", worker=worker_id)

        abbyy_tokens, abbyy_blocks = [], []
        if has_abbyy:
            abbyy_tokens, abbyy_blocks = parse_abbyy_page(axml, page_index=pg - 1)

        try:
            local = process_page_local(
                ark_id, pg, actual_pages, unt_ocr, issue,
                tess_lang=tess_lang,
                abbyy_page_tokens=abbyy_tokens,
                abbyy_blocks=abbyy_blocks,
                worker_id=worker_id,
            )
            local_results[pg] = local
            tprint(f"    p{pg:02d} ✓ [{local['summary']}]", worker=worker_id)
        except Exception as e:
            tprint(f"    p{pg:02d} ✗ local: {e}", worker=worker_id)
            corrected_pages[pg] = f"[CORRECTION FAILED: {e}]\n\n{unt_ocr}"

    # Summary of disputes across all pages
    total_disputes = sum(len(r.get("all_disputes", []))
                         for r in local_results.values())
    pages_needing_claude = sum(1 for r in local_results.values()
                               if len(r.get("all_disputes", [])) > 0
                               or r.get("no_image"))
    tprint(f"  Local OCR done: {len(local_results)} pages, "
           f"{total_disputes} disputes, "
           f"{pages_needing_claude} page(s) need Claude",
           worker=worker_id)

    # ── Phase B: CLAUDE stages (8-9) ─────────────────────────────────────
    for pg in sorted(local_results.keys()):
        local = local_results[pg]
        try:
            corrected, summary = process_page_claude(
                local, ark_id, actual_pages, issue,
                api_key, correction_prompt, rate_limiter)
            corrected_pages[pg] = corrected
            snippet = corrected[:60].replace("\n", " ")
            tprint(f"    p{pg:02d} ✓ [{summary}]  \"{snippet}...\"", worker=worker_id)
        except Exception as e:
            tprint(f"    p{pg:02d} ✗ claude: {e}", worker=worker_id)
            # Fall back to agreed text from local stage
            corrected_pages[pg] = (f"[CORRECTION FAILED: {e}]\n\n"
                                   + local.get("agreed_text", ""))
        time.sleep(delay)

    # Stage 9: Proofreading pass
    proofread_count = 0
    for pg in sorted(corrected_pages.keys()):
        text = corrected_pages[pg]
        if text.startswith("[CORRECTION FAILED"):
            continue
        tprint(f"    p{pg:02d} proofreading...", worker=worker_id)
        proofread = proofread_page(pg, text, api_key, rate_limiter)
        if proofread != text:
            corrected_pages[pg] = proofread
            proofread_count += 1
        time.sleep(delay)
    if proofread_count:
        tprint(f"  Proofread: {proofread_count}/{len(corrected_pages)} page(s) revised",
               worker=worker_id)

    # Write corrected/ file (used by translate step)
    out_lines = [header, ""]
    for pg in sorted(corrected_pages.keys()):
        out_lines.append(f"--- Page {pg} of {actual_pages} ---")
        out_lines.append(corrected_pages[pg])
        out_lines.append("")
    corr_path.write_text('\n'.join(out_lines), encoding="utf-8")
    tprint(f"  → corrected/{fname}  ({corr_path.stat().st_size//1024}KB)",
           worker=worker_id)

    # ── Stage 10a: Article segmentation ─────────────────────────────────────
    tprint(f"  Segmenting...", worker=worker_id)
    all_items = []
    for pg in sorted(corrected_pages.keys()):
        text = corrected_pages[pg]
        if text.startswith("[CORRECTION FAILED"):
            continue
        items = segment_page(pg, text, api_key, rate_limiter)
        all_items.extend(items)
        tprint(f"    p{pg:02d} → {len(items)} item(s)", worker=worker_id)
        time.sleep(delay)

    # ── Stage 10b: Cross-page stitching ─────────────────────────────────────
    if len(corrected_pages) > 1 and all_items:
        tprint(f"  Stitching boundaries...", worker=worker_id)
        all_items = stitch_all_pages(all_items, api_key, rate_limiter, worker_id)

    # ── Stage 11: Write article files ───────────────────────────────────────
    n = write_article_files(issue, all_items, ark_dir)
    tprint(f"  → articles/{ark_id}/  ({n} files)", worker=worker_id)
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
    args = p.parse_args()

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
        wid    = f"w{idx % effective_workers + 1}" if effective_workers > 1 else ""
        tprint(f"[{idx+1:02d}/{len(issues)}] {ark_id}  "
               f"Vol.{issue.get('volume','?')} No.{issue.get('number','?')}  "
               f"{issue.get('date','')}", worker=wid)
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

    # ── Process all issues ────────────────────────────────────────────────
    # process_issue() runs local OCR (stages 1-7) first for all pages in
    # an issue, reports dispute counts, then runs Claude (stages 8-9).
    # Cost estimation happens inside process_issue before Claude calls.
    all_items = list(enumerate(issues))
    if effective_workers == 1:
        for item in all_items: run_issue(item)
    else:
        with ThreadPoolExecutor(max_workers=effective_workers) as ex:
            futs = {ex.submit(run_issue, item): item for item in all_items}
            for fut in as_completed(futs):
                try: fut.result()
                except Exception as e:
                    _, iss = futs[fut]
                    tprint(f"  ✗ Unhandled: {iss['ark_id']}: {e}")

    if rate_limiter: tprint(f"\nRate limiter: {rate_limiter.status_line()}")
    tprint(f"\n{'='*50}")
    tprint(f"Complete: {ctr['ok']}  Skipped: {ctr['skipped']}  Errors: {ctr['err']}")
    tprint(f"Corrected: {CORRECTED_DIR}")
    tprint(f"Articles:  {ARTICLES_DIR}")

if __name__ == "__main__":
    main()
