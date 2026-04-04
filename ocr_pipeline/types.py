"""
ocr_pipeline.types — Data structures for batch-aware OCR preprocessing.

All types are plain dicts or dataclasses — no pydantic dependency.
Schemas are documented here as the contract between pipeline stages.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional
import json
from pathlib import Path


# ── Page-level metadata captured at ingestion ─────────────────────────────

@dataclass
class PageMeta:
    """Metadata for a single page image, captured at ingestion."""
    ark_id: str
    page_num: int
    total_pages: int
    issue_date: str                  # ISO format: YYYY-MM-DD
    volume: str
    number: str
    image_path: str                  # absolute path to cached JPEG
    width: int = 0                   # pixels, set after image load
    height: int = 0
    file_size_bytes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PageMeta":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Style signature: typography + degradation priors per issue ────────────

@dataclass
class StyleSignature:
    """
    Captured from Pass A fast sweep. One per issue (or per page).
    Used for clustering issues by visual similarity and tuning
    preprocessing parameters.
    """
    ark_id: str
    issue_date: str

    # Typography estimates (from connected component analysis)
    median_char_height: float = 0.0       # pixels
    char_height_std: float = 0.0
    median_stroke_width: float = 0.0      # pixels
    stroke_width_std: float = 0.0

    # Illumination / degradation
    bg_intensity_mean: float = 0.0        # 0-255, background after border trim
    bg_intensity_std: float = 0.0
    fg_intensity_mean: float = 0.0        # foreground (ink) mean
    contrast_ratio: float = 0.0           # bg_mean / fg_mean

    # Layout
    estimated_columns: int = 0
    content_area_fraction: float = 0.0    # content pixels / total pixels

    # Sample counts for confidence in these estimates
    n_components_sampled: int = 0
    n_pages_sampled: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StyleSignature":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Preprocessing parameters: tuned per issue/cluster ─────────────────────

@dataclass
class PreprocParams:
    """
    Preprocessing parameters derived from batch statistics.
    Replaces hardcoded values in preprocess_image().
    """
    # CLAHE parameters (existing defaults as baseline)
    clahe_clip_limit: float = 2.5
    clahe_tile_size: int = 8

    # Median blur kernel (must be odd)
    median_blur_k: int = 3

    # Binarization threshold (for connected component analysis, not OCR input)
    binary_threshold: int = 128

    # Content bounds threshold (existing default = 60)
    border_threshold: int = 60

    # Source: how were these derived?
    source: str = "default"           # "default" | "batch_calibrated" | "manual"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PreprocParams":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ── Confidence record: per-word data from alignment stage ─────────────────

@dataclass
class ConfidenceRecord:
    """
    Per-word confidence data extracted after align_sources() + split_agree_dispute().
    This is the data that enables --reprocess-low-confidence.
    """
    ark_id: str
    page_num: int
    column: int
    word_index: int               # position within column
    text: str                     # best reading
    confidence: int               # 0-100
    agreed: bool                  # True if all sources agreed
    source_count: int             # how many engines produced a reading
    top: int = 0                  # bounding box (column-local)
    left: int = 0
    right: int = 0
    bottom: int = 0
    dispute_reason: str = ""      # empty if agreed

    def to_dict(self) -> dict:
        return asdict(self)


# ── Low-confidence region: identified in Pass A for Pass B targeting ──────

@dataclass
class LowConfidenceRegion:
    """
    A region (line or word group) flagged for heavy processing in Pass B.
    """
    ark_id: str
    page_num: int
    column: int
    top: int                      # bounding box in column-local coords
    left: int
    right: int
    bottom: int
    reason: str                   # "disagreement" | "low_conf" | "illegible"
    provisional_text: str = ""    # best guess from Pass A
    mean_confidence: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ── Batch-level summary ──────────────────────────────────────────────────

@dataclass
class BatchSummary:
    """
    Aggregate statistics from a Pass A sweep across multiple issues.
    """
    collection_title: str
    n_issues: int = 0
    n_pages: int = 0
    n_words_total: int = 0
    n_words_high_conf: int = 0
    n_words_low_conf: int = 0
    n_words_illegible: int = 0
    mean_confidence: float = 0.0

    # Per-issue style signatures (populated after sweep)
    issue_signatures: list = field(default_factory=list)

    def high_conf_fraction(self) -> float:
        if self.n_words_total == 0:
            return 0.0
        return self.n_words_high_conf / self.n_words_total

    def to_dict(self) -> dict:
        d = asdict(self)
        d["high_conf_fraction"] = self.high_conf_fraction()
        return d

    def save(self, path: Path):
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "BatchSummary":
        d = json.loads(path.read_text(encoding="utf-8"))
        d.pop("high_conf_fraction", None)
        sigs = d.pop("issue_signatures", [])
        obj = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        obj.issue_signatures = sigs
        return obj
