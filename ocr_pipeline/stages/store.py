"""
ocr_pipeline.stages.store — Phase 5: Feature Store.

Persists and queries style signatures, confidence data, and
low-confidence region indices. Built on top of ArtifactStore
(ocr_pipeline.artifacts) which handles file I/O.

Key capabilities:
  - Store and retrieve per-page confidence records
  - Store and query low-confidence regions for Pass B targeting
  - Find similar issues by style signature (temporal + visual similarity)
  - Compute batch-wide aggregate statistics
"""

import math
import numpy as np
from pathlib import Path

from ocr_pipeline.types import (
    StyleSignature, ConfidenceRecord, LowConfidenceRegion, BatchSummary,
    PreprocParams,
)
from ocr_pipeline.artifacts import ArtifactStore
from ocr_pipeline.config import CONF_THRESHOLD, HC_GATE_CONFIDENCE


# ── Confidence record extraction ──────────────────────────────────────────

def extract_confidence_records(ark_id: str, page_num: int,
                               aligned_words: list,
                               column: int = 0) -> list:
    """
    Convert align_sources() output into ConfidenceRecord objects.

    This is the bridge between unt_ocr_correct.py's alignment stage
    and the batch pipeline's confidence store.

    Args:
        ark_id: Issue ARK identifier
        page_num: 1-indexed page number
        aligned_words: Output from align_sources() — list of AlignedWord dicts
        column: Column number (1-indexed)

    Returns:
        List of ConfidenceRecord objects.
    """
    records = []
    for i, aw in enumerate(aligned_words):
        tokens = aw.get("tokens", {})
        consensus = aw.get("consensus", "")
        agreed = aw.get("agree", False)
        reason = aw.get("dispute_reason", "")

        # Find the best confidence across sources
        best_conf = 0
        best_top = best_left = best_right = best_bottom = 0
        n_sources = 0

        for source, tok in tokens.items():
            if tok is None:
                continue
            n_sources += 1
            conf = tok.get("conf", 0)
            if conf > best_conf:
                best_conf = conf
                best_top = tok.get("top", 0)
                best_left = tok.get("left", 0)
                best_right = tok.get("right", 0)
                best_bottom = tok.get("bottom", 0)

        records.append(ConfidenceRecord(
            ark_id=ark_id,
            page_num=page_num,
            column=column,
            word_index=i,
            text=consensus,
            confidence=best_conf,
            agreed=agreed,
            source_count=n_sources,
            top=best_top,
            left=best_left,
            right=best_right,
            bottom=best_bottom,
            dispute_reason=reason,
        ))

    return records


# ── Low-confidence region identification ──────────────────────────────────

def identify_low_confidence_regions(records: list,
                                    conf_threshold: int = CONF_THRESHOLD,
                                    group_tolerance: int = 20) -> list:
    """
    Group low-confidence words into contiguous regions for Pass B targeting.

    Words with confidence below threshold or that are disputed are
    grouped by vertical proximity (within group_tolerance pixels)
    into LowConfidenceRegion objects.

    Args:
        records: List of ConfidenceRecord objects for one page
        conf_threshold: Below this = low confidence
        group_tolerance: Vertical pixel distance for grouping

    Returns:
        List of LowConfidenceRegion objects.
    """
    low = [r for r in records
           if r.confidence < conf_threshold or not r.agreed]

    if not low:
        return []

    # Sort by column then top position
    low.sort(key=lambda r: (r.column, r.top))

    # Group by vertical proximity within same column
    regions = []
    current_group = [low[0]]

    for r in low[1:]:
        prev = current_group[-1]
        if r.column == prev.column and abs(r.top - prev.top) <= group_tolerance:
            current_group.append(r)
        else:
            regions.append(_group_to_region(current_group))
            current_group = [r]
    regions.append(_group_to_region(current_group))

    return regions


def _group_to_region(group: list) -> LowConfidenceRegion:
    """Convert a group of ConfidenceRecords into a LowConfidenceRegion."""
    first = group[0]
    top = min(r.top for r in group)
    left = min(r.left for r in group)
    right = max(r.right for r in group)
    bottom = max(r.bottom for r in group)
    mean_conf = sum(r.confidence for r in group) / len(group)
    text = " ".join(r.text for r in group if r.text)

    # Classify reason
    reasons = set()
    for r in group:
        if r.confidence == 0:
            reasons.add("illegible")
        elif not r.agreed:
            reasons.add("disagreement")
        else:
            reasons.add("low_conf")
    reason = "+".join(sorted(reasons))

    return LowConfidenceRegion(
        ark_id=first.ark_id,
        page_num=first.page_num,
        column=first.column,
        top=top,
        left=left,
        right=right,
        bottom=bottom,
        reason=reason,
        provisional_text=text,
        mean_confidence=round(mean_conf, 1),
    )


# ── Issue similarity scoring ─────────────────────────────────────────────

def issue_similarity(sig_a: StyleSignature, sig_b: StyleSignature,
                     alpha: float = 0.5,
                     tau_days: float = 14.0) -> float:
    """
    Compute similarity between two issues for clustering.

    Combines visual style similarity with temporal proximity:
        sim = alpha * visual_sim + (1 - alpha) * temporal_sim

    Args:
        sig_a, sig_b: StyleSignature objects
        alpha: Weight for visual similarity (0-1)
        tau_days: Temporal decay constant in days

    Returns:
        Similarity score 0.0 - 1.0 (higher = more similar)
    """
    # Temporal similarity
    temporal = 0.5  # default if dates unavailable
    if sig_a.issue_date and sig_b.issue_date:
        try:
            from datetime import datetime
            da = datetime.strptime(sig_a.issue_date, "%Y-%m-%d")
            db = datetime.strptime(sig_b.issue_date, "%Y-%m-%d")
            delta_days = abs((da - db).days)
            temporal = math.exp(-delta_days / tau_days)
        except (ValueError, TypeError):
            pass

    # Visual similarity: normalized feature distance
    visual = _visual_similarity(sig_a, sig_b)

    return alpha * visual + (1 - alpha) * temporal


def _visual_similarity(a: StyleSignature, b: StyleSignature) -> float:
    """
    Compute visual similarity between two style signatures.
    Uses normalized feature differences with tolerances.
    """
    scores = []

    # Character height (tolerant — different pages vary)
    if a.median_char_height > 0 and b.median_char_height > 0:
        diff = abs(a.median_char_height - b.median_char_height)
        s = max(0, 1.0 - diff / max(a.median_char_height, b.median_char_height))
        scores.append(s)

    # Stroke width
    if a.median_stroke_width > 0 and b.median_stroke_width > 0:
        diff = abs(a.median_stroke_width - b.median_stroke_width)
        s = max(0, 1.0 - diff / max(a.median_stroke_width, b.median_stroke_width))
        scores.append(s)

    # Contrast ratio
    if a.contrast_ratio > 0 and b.contrast_ratio > 0:
        diff = abs(a.contrast_ratio - b.contrast_ratio)
        s = max(0, 1.0 - diff / max(a.contrast_ratio, b.contrast_ratio))
        scores.append(s)

    # Background intensity
    if a.bg_intensity_mean > 0 and b.bg_intensity_mean > 0:
        diff = abs(a.bg_intensity_mean - b.bg_intensity_mean)
        s = max(0, 1.0 - diff / 128.0)  # normalized to half the intensity range
        scores.append(s)

    if not scores:
        return 0.5  # no data to compare

    return sum(scores) / len(scores)


def find_similar_issues(target_sig: StyleSignature,
                        all_sigs: list,
                        top_k: int = 5,
                        alpha: float = 0.5,
                        tau_days: float = 14.0) -> list:
    """
    Find the top-K most similar issues to a target.

    Returns list of (similarity_score, StyleSignature) tuples,
    sorted by similarity descending.
    """
    scored = []
    for sig in all_sigs:
        if sig.ark_id == target_sig.ark_id:
            continue
        score = issue_similarity(target_sig, sig, alpha, tau_days)
        scored.append((score, sig))

    scored.sort(key=lambda x: -x[0])
    return scored[:top_k]


# ── Batch summary computation ─────────────────────────────────────────────

def compute_batch_summary(store: ArtifactStore,
                          title: str,
                          issues: list) -> BatchSummary:
    """
    Compute aggregate statistics from stored confidence data.

    Args:
        store: ArtifactStore with confidence data from Pass A
        title: Collection title
        issues: List of issue dicts

    Returns:
        BatchSummary with aggregate stats.
    """
    summary = BatchSummary(collection_title=title, n_issues=len(issues))

    all_confs = []
    for issue in issues:
        ark_id = issue["ark_id"]
        n_pages = int(issue.get("pages", 8))
        for pg in range(1, n_pages + 1):
            records = store.load_page_confidence(ark_id, pg)
            if not records:
                continue

            summary.n_pages += 1
            for r in records:
                conf = r.get("confidence", 0) if isinstance(r, dict) else r.confidence
                text = r.get("text", "") if isinstance(r, dict) else r.text
                agreed = r.get("agreed", False) if isinstance(r, dict) else r.agreed

                summary.n_words_total += 1
                all_confs.append(conf)

                if text == "[unleserlich]" or conf == 0:
                    summary.n_words_illegible += 1
                elif conf >= HC_GATE_CONFIDENCE and agreed:
                    summary.n_words_high_conf += 1
                else:
                    summary.n_words_low_conf += 1

    if all_confs:
        summary.mean_confidence = round(float(np.mean(all_confs)), 1)

    # Load style signatures
    sigs = store.load_style_signatures()
    summary.issue_signatures = sigs

    return summary


# ── Preprocessing parameter calibration ──────────────────────────────────

def calibrate_preproc_params(signature: StyleSignature) -> PreprocParams:
    """
    Derive preprocessing parameters from a style signature.

    This is the key batch-aware adaptation: instead of hardcoded
    CLAHE/threshold values, we tune them based on measured image
    characteristics.
    """
    params = PreprocParams(source="batch_calibrated")

    # CLAHE clip limit: higher for low-contrast images
    if signature.contrast_ratio > 0:
        if signature.contrast_ratio < 1.5:
            params.clahe_clip_limit = 4.0   # aggressive for faded images
        elif signature.contrast_ratio < 2.0:
            params.clahe_clip_limit = 3.0   # moderate
        else:
            params.clahe_clip_limit = 2.0   # gentle for good contrast

    # CLAHE tile size: larger for larger characters
    if signature.median_char_height > 30:
        params.clahe_tile_size = 16
    elif signature.median_char_height > 15:
        params.clahe_tile_size = 8
    else:
        params.clahe_tile_size = 4

    # Binarization threshold: adapt to background brightness
    if signature.bg_intensity_mean > 180:
        params.binary_threshold = 140
    elif signature.bg_intensity_mean > 120:
        params.binary_threshold = 110
    else:
        params.binary_threshold = 80

    # Border detection: adapt to overall brightness
    if signature.bg_intensity_mean > 0:
        params.border_threshold = max(30, int(signature.fg_intensity_mean * 0.8))

    return params
