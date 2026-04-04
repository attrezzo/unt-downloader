"""
ocr_pipeline.stages.features — Phase 4: Feature Extraction.

Extracts typography and degradation features from the sweep output:
  - Character height distribution (median, std)
  - Stroke width distribution (from distance transform)
  - Layout statistics (column count estimate, line spacing)
  - Ink/background intensity profiles

These features populate StyleSignature records used for:
  - Clustering issues by visual similarity
  - Tuning preprocessing parameters per cluster
  - Detecting press/typeface changes over time
"""

import numpy as np

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

try:
    from scipy.ndimage import distance_transform_edt
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from ocr_pipeline.types import StyleSignature
from ocr_pipeline.config import STROKE_WIDTH_BINS


# ── Character height estimation ───────────────────────────────────────────

def character_height_stats(components: list) -> "tuple[float, float]":
    """
    Estimate character height from connected components.

    Filters to "text-like" components: aspect ratio 0.2-3.0 (excludes
    very wide rules and very tall artifacts). Returns (median, std).

    Components should come from sweep.extract_components() which already
    filters by area bounds.
    """
    heights = [c["height"] for c in components
               if 0.2 <= c["aspect_ratio"] <= 3.0]

    if len(heights) < 10:
        return 0.0, 0.0

    return float(np.median(heights)), float(np.std(heights))


# ── Stroke width estimation ──────────────────────────────────────────────

def stroke_width_stats(binary: np.ndarray,
                       components: list,
                       sample_limit: int = 200) -> "tuple[float, float]":
    """
    Estimate stroke width from the distance transform of ink regions.

    For each sampled character component, compute the distance transform
    of its ink pixels. The median of the maximum distances across
    components gives the typical stroke half-width (multiply by 2 for
    full stroke width).

    This is a fast approximation of the Stroke Width Transform (SWT).
    It works well for printed text where stroke width is fairly uniform
    within a character.

    Args:
        binary: Binary image (255 = ink)
        components: From sweep.extract_components()
        sample_limit: Max components to sample (for speed)

    Returns:
        (median_stroke_width, std_stroke_width) in pixels.
        Returns (0, 0) if scipy unavailable or insufficient data.
    """
    if not HAS_SCIPY or not HAS_CV2:
        return 0.0, 0.0

    # Sample text-like components
    text_comps = [c for c in components if 0.2 <= c["aspect_ratio"] <= 3.0]
    if len(text_comps) > sample_limit:
        rng = np.random.default_rng(42)
        indices = rng.choice(len(text_comps), sample_limit, replace=False)
        text_comps = [text_comps[i] for i in indices]

    if len(text_comps) < 10:
        return 0.0, 0.0

    max_dists = []
    for comp in text_comps:
        x, y = comp["left"], comp["top"]
        w, h = comp["width"], comp["height"]

        # Crop the component from binary
        crop = binary[y:y+h, x:x+w]
        if crop.size == 0:
            continue

        # Distance transform: distance from each ink pixel to nearest bg
        ink_mask = (crop > 0).astype(np.uint8)
        if ink_mask.sum() < 4:
            continue

        dt = distance_transform_edt(ink_mask)
        # The max distance = half the stroke width at the thickest point
        max_d = float(dt.max())
        if max_d > 0:
            max_dists.append(max_d * 2.0)  # full stroke width

    if len(max_dists) < 5:
        return 0.0, 0.0

    return float(np.median(max_dists)), float(np.std(max_dists))


# ── Ink/background intensity profiles ─────────────────────────────────────

def intensity_profile(img_gray: np.ndarray,
                      binary: np.ndarray) -> "tuple[float, float, float, float, float]":
    """
    Measure foreground (ink) and background intensity distributions.

    Returns:
        (bg_mean, bg_std, fg_mean, fg_std, contrast_ratio)

    contrast_ratio = bg_mean / max(fg_mean, 1). Higher = better separation.
    For good microfilm: contrast_ratio > 2.0.
    For degraded/faded: contrast_ratio < 1.5.
    """
    fg_mask = binary > 0
    bg_mask = ~fg_mask

    fg_pixels = img_gray[fg_mask]
    bg_pixels = img_gray[bg_mask]

    if len(fg_pixels) == 0 or len(bg_pixels) == 0:
        return 128.0, 50.0, 64.0, 30.0, 2.0  # neutral defaults

    bg_mean = float(np.mean(bg_pixels))
    bg_std = float(np.std(bg_pixels))
    fg_mean = float(np.mean(fg_pixels))
    fg_std = float(np.std(fg_pixels))
    contrast = bg_mean / max(fg_mean, 1.0)

    return bg_mean, bg_std, fg_mean, fg_std, contrast


# ── Line spacing estimation ───────────────────────────────────────────────

def estimate_line_spacing(components: list,
                          min_comps: int = 30) -> "tuple[float, float]":
    """
    Estimate vertical line spacing from component y-coordinates.

    Groups components into rows by y-proximity, then measures distances
    between consecutive row centroids.

    Returns:
        (median_line_spacing, std_line_spacing) in pixels.
        (0, 0) if insufficient data.
    """
    if len(components) < min_comps:
        return 0.0, 0.0

    # Sort by y-centroid
    ys = sorted(c["cy"] for c in components)

    # Cluster into rows: merge y values within tolerance
    # Tolerance = median character height / 2
    heights = [c["height"] for c in components if 0.2 <= c["aspect_ratio"] <= 3.0]
    if not heights:
        return 0.0, 0.0
    tol = float(np.median(heights)) / 2

    rows = []
    current_row = [ys[0]]
    for y in ys[1:]:
        if y - current_row[-1] <= tol:
            current_row.append(y)
        else:
            rows.append(float(np.mean(current_row)))
            current_row = [y]
    rows.append(float(np.mean(current_row)))

    if len(rows) < 3:
        return 0.0, 0.0

    spacings = [rows[i+1] - rows[i] for i in range(len(rows) - 1)]
    return float(np.median(spacings)), float(np.std(spacings))


# ── Content area fraction ─────────────────────────────────────────────────

def content_fraction(binary: np.ndarray) -> float:
    """Fraction of the image that is foreground (ink)."""
    return float(np.sum(binary > 0)) / max(binary.size, 1)


# ── Build StyleSignature from sweep output ────────────────────────────────

def build_style_signature(ark_id: str, issue_date: str,
                          img_gray: np.ndarray,
                          binary: np.ndarray,
                          components: list,
                          conf_map: np.ndarray) -> StyleSignature:
    """
    Build a complete StyleSignature from a single page's sweep results.

    Call this for each page, then aggregate across pages in an issue
    using aggregate_signatures().
    """
    med_h, std_h = character_height_stats(components)
    med_sw, std_sw = stroke_width_stats(binary, components)
    bg_m, bg_s, fg_m, fg_s, contrast = intensity_profile(img_gray, binary)
    cf = content_fraction(binary)

    # Estimate columns from component x-distribution
    # (rough: count major gaps in x-histogram)
    est_cols = _estimate_column_count(components, img_gray.shape[1])

    return StyleSignature(
        ark_id=ark_id,
        issue_date=issue_date,
        median_char_height=round(med_h, 1),
        char_height_std=round(std_h, 1),
        median_stroke_width=round(med_sw, 1),
        stroke_width_std=round(std_sw, 1),
        bg_intensity_mean=round(bg_m, 1),
        bg_intensity_std=round(bg_s, 1),
        fg_intensity_mean=round(fg_m, 1),
        contrast_ratio=round(contrast, 2),
        estimated_columns=est_cols,
        content_area_fraction=round(cf, 4),
        n_components_sampled=len(components),
        n_pages_sampled=1,
    )


def _estimate_column_count(components: list, image_width: int) -> int:
    """
    Rough column count estimate from component x-distribution.
    Looks for major vertical gaps in the x-histogram.
    """
    if len(components) < 20 or image_width < 200:
        return 1

    xs = np.array([c["cx"] for c in components])
    # Histogram with bins roughly 1% of image width
    n_bins = max(image_width // 10, 50)
    hist, edges = np.histogram(xs, bins=n_bins, range=(0, image_width))

    # Smooth
    if len(hist) > 5:
        kernel = np.ones(5) / 5
        hist_smooth = np.convolve(hist, kernel, mode="same")
    else:
        hist_smooth = hist.astype(float)

    # Find significant gaps (runs of near-zero bins)
    threshold = max(hist_smooth.max() * 0.1, 1)
    is_gap = hist_smooth < threshold

    # Count transitions from content to gap
    n_gaps = 0
    in_gap = False
    gap_width = 0
    min_gap_bins = max(n_bins // 20, 2)  # gap must span 5%+ of width

    for i, g in enumerate(is_gap):
        if g:
            gap_width += 1
            if not in_gap and gap_width >= min_gap_bins:
                in_gap = True
                n_gaps += 1
        else:
            in_gap = False
            gap_width = 0

    return n_gaps + 1  # n gaps = n+1 columns


def aggregate_signatures(signatures: list) -> StyleSignature:
    """
    Aggregate multiple page-level StyleSignatures into one issue-level signature.
    Uses median for robustness against outlier pages (ads, mastheads).
    """
    if not signatures:
        return StyleSignature(ark_id="", issue_date="")
    if len(signatures) == 1:
        return signatures[0]

    # Use the first signature's identity
    result = StyleSignature(
        ark_id=signatures[0].ark_id,
        issue_date=signatures[0].issue_date,
    )

    def med(attr):
        vals = [getattr(s, attr) for s in signatures if getattr(s, attr) > 0]
        return round(float(np.median(vals)), 2) if vals else 0.0

    result.median_char_height = med("median_char_height")
    result.char_height_std = med("char_height_std")
    result.median_stroke_width = med("median_stroke_width")
    result.stroke_width_std = med("stroke_width_std")
    result.bg_intensity_mean = med("bg_intensity_mean")
    result.bg_intensity_std = med("bg_intensity_std")
    result.fg_intensity_mean = med("fg_intensity_mean")
    result.contrast_ratio = med("contrast_ratio")
    result.content_area_fraction = med("content_area_fraction")

    # Columns: mode (most common estimate)
    col_counts = [s.estimated_columns for s in signatures if s.estimated_columns > 0]
    if col_counts:
        vals, counts = np.unique(col_counts, return_counts=True)
        result.estimated_columns = int(vals[np.argmax(counts)])

    result.n_components_sampled = sum(s.n_components_sampled for s in signatures)
    result.n_pages_sampled = len(signatures)

    return result
