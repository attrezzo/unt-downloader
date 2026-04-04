"""
ocr_pipeline.stages.ingest — Phase 2: Image Ingestion.

Loads page images from the existing image cache (populated by
unt_archive_downloader.py --preload-images) and captures metadata
needed by downstream pipeline stages.

Does NOT download images — that remains the downloader's job.
This stage reads from the cache and produces PageMeta records.
"""

import json
from pathlib import Path

import numpy as np

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False

from ocr_pipeline.types import PageMeta
from ocr_pipeline.logging_utils import pipeline_log


# Minimum file size for a valid scan (matches unt_ocr_correct.py)
MIN_IMAGE_BYTES = 50_000


def image_path(images_dir: Path, ark_id: str, page_num: int) -> Path:
    """Canonical path to a cached page image."""
    return images_dir / ark_id / f"page_{page_num:02d}.jpg"


def is_valid_image(path: Path) -> bool:
    """True if file exists and is large enough to be a real scan."""
    return path.exists() and path.stat().st_size >= MIN_IMAGE_BYTES


def load_image_gray(path: Path) -> "np.ndarray | None":
    """Load a page image as grayscale numpy array. Returns None on failure."""
    if not HAS_CV2:
        pipeline_log("cv2 not available — cannot load images", level="error")
        return None
    if not path.exists():
        return None
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None or img.size == 0:
        return None
    return img


def ingest_page(images_dir: Path, issue: dict, page_num: int) -> "tuple[PageMeta, np.ndarray | None]":
    """
    Load one page image and build its PageMeta record.

    Args:
        images_dir: Path to collection's images/ directory
        issue: Issue dict from all_issues.json
        page_num: 1-indexed page number

    Returns:
        (page_meta, img_gray) — img_gray is None if image unavailable
    """
    ark_id = issue["ark_id"]
    total_pages = int(issue.get("pages", 8))
    img_path = image_path(images_dir, ark_id, page_num)

    meta = PageMeta(
        ark_id=ark_id,
        page_num=page_num,
        total_pages=total_pages,
        issue_date=issue.get("date", ""),
        volume=str(issue.get("volume", "?")),
        number=str(issue.get("number", "?")),
        image_path=str(img_path),
    )

    if not is_valid_image(img_path):
        pipeline_log(f"  {ark_id} p{page_num:02d}: image not available", level="warn")
        return meta, None

    meta.file_size_bytes = img_path.stat().st_size
    img = load_image_gray(img_path)

    if img is not None:
        meta.height, meta.width = img.shape[:2]

    return meta, img


def ingest_issue(images_dir: Path, issue: dict) -> "list[tuple[PageMeta, np.ndarray | None]]":
    """
    Load all page images for one issue.

    Returns list of (PageMeta, img_gray_or_None) tuples, one per page.
    """
    total_pages = int(issue.get("pages", 8))
    results = []
    for pg in range(1, total_pages + 1):
        results.append(ingest_page(images_dir, issue, pg))
    return results


def load_issue_index(metadata_dir: Path) -> list:
    """Load all_issues.json. Returns list of issue dicts."""
    index_path = metadata_dir / "all_issues.json"
    if not index_path.exists():
        return []
    with open(index_path, encoding="utf-8") as f:
        return json.load(f)
