"""
ocr_pipeline.stages.ocr_probe — Phase 6: OCR Probe on Strong Regions.

Runs Tesseract OCR on high-confidence regions identified by the sweep
to extract reliable text + per-word confidence data.

This is a lightweight OCR pass — it does NOT run the full multi-engine
pipeline from unt_ocr_correct.py. It runs Tesseract only, on regions
where the sweep's confidence map indicates strong text signal.

Purpose:
  - Populate the confidence store with per-word data
  - Identify high-confidence pseudo-labels for batch learning
  - Map which regions need the full heavy pipeline (Pass B)

The probe uses the flattened/enhanced image from the sweep stage,
NOT the raw page image. This gives Tesseract the best possible input
for the fast pass.
"""

import numpy as np

try:
    import pytesseract
    from PIL import Image
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

from ocr_pipeline.types import ConfidenceRecord
from ocr_pipeline.config import CONF_THRESHOLD, HC_GATE_CONFIDENCE
from ocr_pipeline.logging_utils import pipeline_log


# Tesseract config for the probe pass (single column, LSTM engine, 300dpi)
PROBE_TESS_CONFIG = "--psm 6 --oem 1 --dpi 300"

# The canonical illegible marker (must match unt_ocr_correct.py)
ILLEGIBLE = "[unleserlich]"


def detect_tesseract_lang() -> "str | None":
    """
    Find the best available Tesseract language string.
    Priority: deu_frak+deu > deu_frak > deu > eng

    Returns None if Tesseract is not available.
    """
    if not HAS_TESSERACT:
        return None

    priority = ["deu_frak+deu", "deu_frak", "deu", "eng"]
    try:
        available = pytesseract.get_languages()
    except Exception:
        return None

    for lang_str in priority:
        parts = lang_str.replace("+", " ").split()
        if all(p in available for p in parts):
            return lang_str
    return None


def probe_region(img_gray: np.ndarray,
                 lang: str,
                 config: str = PROBE_TESS_CONFIG) -> "list[dict]":
    """
    Run Tesseract on an image region and return per-word results.

    Args:
        img_gray: Grayscale image region (numpy array)
        lang: Tesseract language string
        config: Tesseract config string

    Returns:
        List of word dicts: {text, conf, left, top, right, bottom}
        Empty list on any error.
    """
    if not HAS_TESSERACT or not HAS_CV2:
        return []

    try:
        pil_img = Image.fromarray(img_gray)
        data = pytesseract.image_to_data(
            pil_img, lang=lang, config=config, output_type=pytesseract.Output.DICT)
    except Exception as e:
        pipeline_log(f"  Tesseract probe error: {e}", level="warn")
        return []

    words = []
    n = len(data["text"])
    for i in range(n):
        text = data["text"][i].strip()
        conf = int(data["conf"][i])

        if conf < 0 or not text:
            continue

        # Very low confidence → illegible
        if conf < 10:
            text = ILLEGIBLE
            conf = 0

        words.append({
            "text": text,
            "conf": conf,
            "left": int(data["left"][i]),
            "top": int(data["top"][i]),
            "right": int(data["left"][i]) + int(data["width"][i]),
            "bottom": int(data["top"][i]) + int(data["height"][i]),
        })

    return words


def probe_page(flattened: np.ndarray,
               conf_map: np.ndarray,
               ark_id: str,
               page_num: int,
               lang: str,
               conf_threshold: float = 0.3) -> "tuple[list[ConfidenceRecord], dict]":
    """
    Run OCR probe on high-confidence regions of a page.

    Uses the confidence map from sweep to decide which grid cells
    to probe. Only cells with confidence > conf_threshold are probed.

    Args:
        flattened: Illumination-corrected grayscale from sweep
        conf_map: (rows, cols) confidence grid from sweep
        ark_id: Issue ARK identifier
        page_num: 1-indexed page number
        lang: Tesseract language string
        conf_threshold: Min sweep confidence to probe a cell

    Returns:
        (records, stats) where:
          records: list of ConfidenceRecord objects
          stats: dict with probe summary
    """
    if not HAS_TESSERACT:
        return [], {"error": "tesseract_not_available"}

    h, w = flattened.shape
    grid_rows, grid_cols = conf_map.shape
    cell_h = h // grid_rows
    cell_w = w // grid_cols

    all_records = []
    cells_probed = 0
    cells_skipped = 0
    total_words = 0
    high_conf_words = 0

    for r in range(grid_rows):
        for c in range(grid_cols):
            if conf_map[r, c] < conf_threshold:
                cells_skipped += 1
                continue

            cells_probed += 1
            y1, y2 = r * cell_h, (r + 1) * cell_h
            x1, x2 = c * cell_w, (c + 1) * cell_w
            region = flattened[y1:y2, x1:x2]

            if region.shape[0] < 20 or region.shape[1] < 20:
                continue

            words = probe_region(region, lang)

            for i, word in enumerate(words):
                total_words += 1
                is_high = word["conf"] >= HC_GATE_CONFIDENCE
                if is_high:
                    high_conf_words += 1

                record = ConfidenceRecord(
                    ark_id=ark_id,
                    page_num=page_num,
                    column=0,      # grid-based, not column-based
                    word_index=len(all_records),
                    text=word["text"],
                    confidence=word["conf"],
                    agreed=True,   # single-engine, no disagreement possible
                    source_count=1,
                    top=word["top"] + y1,      # convert to page coords
                    left=word["left"] + x1,
                    right=word["right"] + x1,
                    bottom=word["bottom"] + y1,
                )
                all_records.append(record)

    stats = {
        "cells_probed": cells_probed,
        "cells_skipped": cells_skipped,
        "total_words": total_words,
        "high_conf_words": high_conf_words,
        "high_conf_fraction": round(high_conf_words / max(total_words, 1), 3),
    }

    return all_records, stats
