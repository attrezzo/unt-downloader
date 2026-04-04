"""
ocr_pipeline.stages.sweep — Phase 3: High-Confidence Sweep.

Fast first pass over page images to extract high-confidence text signals.
This stage does NOT run OCR — it analyzes the image to:
  1. Flatten illumination (remove microfilm vignetting/gradients)
  2. Apply adaptive thresholding to get clean binary
  3. Extract connected components (character-sized blobs)
  4. Classify regions as "strong" (clear ink, good contrast) vs "weak"

The output is:
  - A cleaned/flattened grayscale suitable for OCR
  - A binary mask of high-confidence text regions
  - Connected component statistics (sizes, positions, densities)
  - Per-region confidence scores based on contrast and component quality

This feeds into:
  - Feature extraction (Phase 4) for style signatures
  - OCR probe (Phase 6) which runs Tesseract on strong regions only
  - Pass B targeting (low-confidence region masks)
"""

import numpy as np

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from scipy.ndimage import uniform_filter, median_filter
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from ocr_pipeline.config import CC_MIN_AREA, CC_MAX_AREA
from ocr_pipeline.logging_utils import pipeline_log


# ── Illumination flattening ───────────────────────────────────────────────

def estimate_background(img_gray: np.ndarray, block_size: int = 51) -> np.ndarray:
    """
    Estimate the illumination background of a grayscale image.

    Uses a large morphological closing (bright envelope) to model
    the slow-varying background illumination from microfilm scanning.
    This captures vignetting, uneven lamp exposure, and film density
    gradients without touching the text ink.

    Args:
        img_gray: Grayscale image (uint8)
        block_size: Kernel size for morphological closing. Must be odd.
                    Larger = smoother background estimate. 51 works well
                    for typical 300dpi microfilm scans.

    Returns:
        Background estimate, same shape as input (uint8).
    """
    if not HAS_CV2:
        return np.full_like(img_gray, 200)  # fallback: assume uniform light bg

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (block_size, block_size))
    return cv2.morphologyEx(img_gray, cv2.MORPH_CLOSE, kernel)


def flatten_illumination(img_gray: np.ndarray, block_size: int = 51) -> np.ndarray:
    """
    Remove illumination gradient, producing uniform-background grayscale.

    Divides the image by the estimated background and rescales to 0-255.
    Text ink becomes dark on a uniformly light background regardless of
    original scanning illumination.

    This replaces the simple CLAHE in preprocess_image() with a
    physically-motivated correction. CLAHE is still useful afterward
    for local contrast enhancement, but the heavy lifting of gradient
    removal happens here.

    Args:
        img_gray: Grayscale page image (uint8)
        block_size: Background estimation kernel size (odd integer)

    Returns:
        Flattened grayscale image (uint8), same shape.
    """
    bg = estimate_background(img_gray, block_size)

    # Avoid division by zero in very dark regions
    bg_safe = np.maximum(bg, 1).astype(np.float32)
    flat = img_gray.astype(np.float32) / bg_safe * 200.0
    return np.clip(flat, 0, 255).astype(np.uint8)


# ── Adaptive thresholding ─────────────────────────────────────────────────

def adaptive_threshold(img_gray: np.ndarray, block_size: int = 25,
                       C: int = 12) -> np.ndarray:
    """
    Produce a binary image using Gaussian adaptive thresholding.

    More robust than global Otsu for microfilm with residual gradients.
    Text pixels = 255 (white), background = 0 (black) — inverted from
    OpenCV's convention because we want connected components to be the
    foreground (ink).

    Args:
        img_gray: Grayscale image (uint8), ideally after flatten_illumination
        block_size: Neighborhood size (odd, >= 3). 25 works for 300dpi.
        C: Constant subtracted from mean. Higher = more conservative
           (fewer faint pixels accepted). 12 is conservative for microfilm.

    Returns:
        Binary image (uint8): 255 = ink, 0 = background.
    """
    if not HAS_CV2:
        # Fallback: simple global threshold
        thresh = int(np.mean(img_gray) * 0.6)
        binary = np.zeros_like(img_gray)
        binary[img_gray < thresh] = 255
        return binary

    # OpenCV adaptive threshold gives 255 for foreground (dark=text inverted)
    binary = cv2.adaptiveThreshold(
        img_gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV, block_size, C)
    return binary


# ── Connected component analysis ──────────────────────────────────────────

def extract_components(binary: np.ndarray,
                       min_area: int = CC_MIN_AREA,
                       max_area: int = CC_MAX_AREA) -> "list[dict]":
    """
    Extract connected components from a binary image, filtering to
    character-sized objects.

    Each component is a dict with bounding box, area, and centroid.
    Components below min_area are noise; above max_area are non-text
    artifacts (borders, images, large ink blots).

    Args:
        binary: Binary image (uint8), 255 = foreground (ink)
        min_area: Minimum component area in pixels
        max_area: Maximum component area in pixels

    Returns:
        List of component dicts, sorted top-to-bottom then left-to-right:
        {
            "label": int,         # component ID
            "left": int,
            "top": int,
            "width": int,
            "height": int,
            "area": int,          # actual pixel count, not bbox area
            "cx": int,            # centroid x
            "cy": int,            # centroid y
            "aspect_ratio": float # width/height
        }
    """
    if not HAS_CV2:
        return []

    n_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(
        binary, connectivity=8)

    components = []
    for i in range(1, n_labels):  # skip label 0 (background)
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area or area > max_area:
            continue

        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])

        components.append({
            "label": i,
            "left": int(stats[i, cv2.CC_STAT_LEFT]),
            "top": int(stats[i, cv2.CC_STAT_TOP]),
            "width": w,
            "height": h,
            "area": area,
            "cx": int(centroids[i, 0]),
            "cy": int(centroids[i, 1]),
            "aspect_ratio": w / max(h, 1),
        })

    # Sort: top-to-bottom, then left-to-right
    components.sort(key=lambda c: (c["top"], c["left"]))
    return components


# ── Region confidence scoring ─────────────────────────────────────────────

def score_region_confidence(img_gray: np.ndarray, binary: np.ndarray,
                            components: list,
                            grid_rows: int = 10,
                            grid_cols: int = 10) -> np.ndarray:
    """
    Score image regions by text quality indicators.

    Divides the image into a grid and scores each cell based on:
      - Component density (are there character-sized objects?)
      - Local contrast (is ink clearly separated from background?)
      - Component regularity (are sizes consistent = text, not noise?)

    Returns a (grid_rows, grid_cols) confidence map, values 0.0-1.0.
    High values = strong text signal, good for OCR.
    Low values = faint, noisy, or blank — candidates for Pass B.
    """
    h, w = img_gray.shape
    cell_h = h // grid_rows
    cell_w = w // grid_cols

    conf_map = np.zeros((grid_rows, grid_cols), dtype=np.float32)

    for r in range(grid_rows):
        for c in range(grid_cols):
            y1, y2 = r * cell_h, (r + 1) * cell_h
            x1, x2 = c * cell_w, (c + 1) * cell_w

            # Components in this cell
            cell_comps = [comp for comp in components
                          if x1 <= comp["cx"] < x2 and y1 <= comp["cy"] < y2]

            if not cell_comps:
                conf_map[r, c] = 0.0
                continue

            # Factor 1: component density (normalized)
            cell_area = cell_h * cell_w
            ink_area = sum(comp["area"] for comp in cell_comps)
            density = min(ink_area / max(cell_area, 1), 0.3) / 0.3  # cap at 30%

            # Factor 2: local contrast
            cell_gray = img_gray[y1:y2, x1:x2]
            cell_bin = binary[y1:y2, x1:x2]
            fg_pixels = cell_gray[cell_bin > 0]
            bg_pixels = cell_gray[cell_bin == 0]

            if len(fg_pixels) > 0 and len(bg_pixels) > 0:
                contrast = (float(np.mean(bg_pixels)) - float(np.mean(fg_pixels))) / 255.0
            else:
                contrast = 0.0
            contrast = max(contrast, 0.0)  # negative = inverted, unusual

            # Factor 3: size regularity (lower std = more uniform = more text-like)
            heights = [comp["height"] for comp in cell_comps]
            if len(heights) >= 3:
                h_std = float(np.std(heights))
                h_mean = float(np.mean(heights))
                regularity = 1.0 - min(h_std / max(h_mean, 1), 1.0)
            else:
                regularity = 0.5  # not enough data

            # Weighted combination
            conf_map[r, c] = 0.4 * density + 0.4 * contrast + 0.2 * regularity

    return conf_map


# ── Full sweep for one page ───────────────────────────────────────────────

def sweep_page(img_gray: np.ndarray,
               bg_block_size: int = 51,
               thresh_block_size: int = 25,
               thresh_C: int = 12,
               min_area: int = CC_MIN_AREA,
               max_area: int = CC_MAX_AREA) -> dict:
    """
    Run the full high-confidence sweep on one page image.

    Args:
        img_gray: Grayscale page image (uint8)
        bg_block_size: Background estimation kernel size
        thresh_block_size: Adaptive threshold neighborhood
        thresh_C: Adaptive threshold constant
        min_area: Min connected component area
        max_area: Max connected component area

    Returns:
        {
            "flattened":   np.ndarray,   # illumination-corrected grayscale
            "binary":      np.ndarray,   # binary mask (255=ink)
            "components":  list[dict],   # character-sized connected components
            "conf_map":    np.ndarray,   # (10, 10) confidence grid
            "stats": {
                "n_components":    int,
                "mean_comp_area":  float,
                "mean_comp_height": float,
                "mean_confidence": float,
                "strong_cells":    int,   # cells with conf > 0.5
                "weak_cells":      int,   # cells with conf < 0.2
            }
        }
    """
    flattened = flatten_illumination(img_gray, bg_block_size)
    binary = adaptive_threshold(flattened, thresh_block_size, thresh_C)
    components = extract_components(binary, min_area, max_area)
    conf_map = score_region_confidence(img_gray, binary, components)

    # Compute summary stats
    n = len(components)
    mean_area = float(np.mean([c["area"] for c in components])) if n > 0 else 0.0
    mean_height = float(np.mean([c["height"] for c in components])) if n > 0 else 0.0
    mean_conf = float(np.mean(conf_map))
    strong = int(np.sum(conf_map > 0.5))
    weak = int(np.sum(conf_map < 0.2))

    return {
        "flattened": flattened,
        "binary": binary,
        "components": components,
        "conf_map": conf_map,
        "stats": {
            "n_components": n,
            "mean_comp_area": round(mean_area, 1),
            "mean_comp_height": round(mean_height, 1),
            "mean_confidence": round(mean_conf, 3),
            "strong_cells": strong,
            "weak_cells": weak,
        },
    }
