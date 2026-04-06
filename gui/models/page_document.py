"""
gui/models/page_document.py — All data for one page, assembled from disk.

Loads:
  - ai_ocr/{ark_id}/page_{NN}.md    full Claude output (source of truth for gaps)
  - images/{ark_id}/page_{NN}.jpg   JPEG scan
  - confidence/{ark_id}_page{NN}.json  per-word confidence records (optional)
  - artifacts/style_signatures.json    issue typography profile (optional)

Provides:
  - raw_text       full markdown text as-is
  - gaps           list[GapData] parsed from raw_text
  - word_bboxes    list[dict] with page-absolute bbox per word (for linking)
  - image_path     Path to JPEG
"""

from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .gap import GapData, gaps_from_text, apply_gap_edits
from .char_measure import CharDefaults


# ---------------------------------------------------------------------------
# Word bbox record (normalised from ConfidenceRecord)
# ---------------------------------------------------------------------------

@dataclass
class WordBbox:
    """A word's location on the page in page-absolute pixels."""
    text:       str
    left:       int
    top:        int
    right:      int
    bottom:     int
    confidence: int    # 0-100
    column:     int    # column number (1-indexed)
    word_index: int
    agreed:     bool

    @property
    def x(self) -> int: return self.left
    @property
    def y(self) -> int: return self.top
    @property
    def w(self) -> int: return self.right - self.left
    @property
    def h(self) -> int: return self.bottom - self.top


class PageDocument:
    """All data for one page, ready for the GUI."""

    def __init__(self, ark_id: str, page_num: int, total_pages: int,
                 collection_dir: Path):
        self.ark_id       = ark_id
        self.page_num     = page_num
        self.total_pages  = total_pages
        self.collection_dir = Path(collection_dir)

        self.raw_text:    str        = ""
        self.gaps:        list       = []        # list[GapData]
        self.word_bboxes: list       = []        # list[WordBbox]
        self.image_path:  Optional[Path] = None
        self.char_defaults: CharDefaults = CharDefaults()
        self._dirty = False

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, ark_id: str, page_num: int, total_pages: int,
             collection_dir: Path) -> PageDocument:
        doc = cls(ark_id, page_num, total_pages, collection_dir)
        doc._load_text()
        doc._load_image()
        doc._load_confidence()
        doc._load_style_signature()
        return doc

    def _load_text(self):
        md_path = (self.collection_dir / "ai_ocr" / self.ark_id /
                   f"page_{self.page_num:02d}.md")
        if md_path.exists():
            self.raw_text = md_path.read_text(encoding="utf-8")
        else:
            # Fall back to corrected/ text
            corr_dir = self.collection_dir / "corrected"
            for f in corr_dir.glob(f"{self.ark_id}_*.txt"):
                pages = _extract_page_text(f.read_text(encoding="utf-8"),
                                           self.page_num)
                if pages:
                    self.raw_text = pages
                    break
        self.gaps = gaps_from_text(self.raw_text)

    def _load_image(self):
        img_dir = self.collection_dir / "images" / self.ark_id
        candidates = [
            img_dir / f"page_{self.page_num:02d}.jpg",
            img_dir / f"page_{self.page_num:02d}.jpeg",
            img_dir / f"page_{self.page_num}.jpg",
        ]
        for p in candidates:
            if p.exists():
                self.image_path = p
                return

    def _load_confidence(self):
        conf_path = (self.collection_dir / "confidence" /
                     f"{self.ark_id}_page{self.page_num:02d}.json")
        if not conf_path.exists():
            return
        records = json.loads(conf_path.read_text(encoding="utf-8"))
        self.word_bboxes = _records_to_word_bboxes(records)

    def _load_style_signature(self):
        sig_path = (self.collection_dir / "artifacts" / "style_signatures.json")
        if not sig_path.exists():
            return
        sigs = json.loads(sig_path.read_text(encoding="utf-8"))
        # Use the first signature for the issue (they share one ark_id prefix)
        for s in sigs:
            if s.get("ark_id", "").startswith(self.ark_id[:8]):
                self.char_defaults = CharDefaults.from_style_signature(s)
                return
        if sigs:
            self.char_defaults = CharDefaults.from_style_signature(sigs[0])

    # ------------------------------------------------------------------
    # Editing
    # ------------------------------------------------------------------

    def apply_edits(self):
        """Rewrite raw_text in-place with current gap edit state."""
        self.raw_text = apply_gap_edits(self.raw_text, self.gaps)
        self._dirty = True

    def save(self):
        """Write edited text back to ai_ocr/.md file (atomic)."""
        self.apply_edits()
        md_path = (self.collection_dir / "ai_ocr" / self.ark_id /
                   f"page_{self.page_num:02d}.md")
        md_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = md_path.with_suffix(".md.tmp")
        tmp.write_text(self.raw_text, encoding="utf-8")
        tmp.replace(md_path)
        self._dirty = False

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    # ------------------------------------------------------------------
    # Gap statistics
    # ------------------------------------------------------------------

    def gap_stats(self) -> dict:
        total    = len(self.gaps)
        pending  = sum(1 for g in self.gaps if not g.is_human_edited)
        missing  = sum(1 for g in self.gaps if g.status == "human-missing")
        corrected = sum(1 for g in self.gaps
                        if g.status in ("human-corrected", "human-accepted"))
        return {
            "total":     total,
            "pending":   pending,
            "corrected": corrected,
            "missing":   missing,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _records_to_word_bboxes(records: list) -> list:
    """Convert ConfidenceRecord dicts to WordBbox objects.

    ConfidenceRecord bboxes are column-local. We approximate page-absolute
    coords using the column number: offset_x ≈ column_index * column_width.
    This is imprecise without layout data but good enough for highlighting.
    """
    if not records:
        return []

    # Estimate number of columns and column width from data
    max_col = max((r.get("column", 1) for r in records), default=1)
    # Rough column width from max right coordinate seen
    max_right = max((r.get("right", 0) for r in records), default=1000)
    col_width = max_right if max_col == 1 else max_right

    result = []
    for r in records:
        col = r.get("column", 1)
        col_offset_x = (col - 1) * col_width
        result.append(WordBbox(
            text=r.get("text", ""),
            left=r.get("left", 0) + col_offset_x,
            top=r.get("top", 0),
            right=r.get("right", 0) + col_offset_x,
            bottom=r.get("bottom", 0),
            confidence=r.get("confidence", 0),
            column=col,
            word_index=r.get("word_index", 0),
            agreed=r.get("agreed", True),
        ))
    return result


_PAGE_MARKER_RE = re.compile(r'^--- Page (\d+) of (\d+) ---$', re.MULTILINE)


def _extract_page_text(full_text: str, page_num: int) -> str:
    """Extract text for a single page from a multi-page corrected/ file."""
    markers = list(_PAGE_MARKER_RE.finditer(full_text))
    for i, m in enumerate(markers):
        if int(m.group(1)) == page_num:
            start = m.end()
            end = markers[i + 1].start() if i + 1 < len(markers) else len(full_text)
            return full_text[start:end].strip()
    return ""
