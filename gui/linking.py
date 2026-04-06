"""
gui/linking.py — Bidirectional text ↔ image coordinate mapping.

Builds an index from the ConfidenceRecord word_bboxes and gap imgbbox fields,
allowing:
  - text_cursor → image bbox (highlight the word/gap under the cursor)
  - image click (x,y) → nearest word/gap in text (scroll to it)
"""

from __future__ import annotations
import re
from typing import Optional

from .models.page_document import PageDocument, WordBbox
from .models.gap import GapData


# ---------------------------------------------------------------------------
# Index entry
# ---------------------------------------------------------------------------

class BboxEntry:
    """Maps a text region to an image bbox."""
    __slots__ = ("text_start", "text_end", "bbox", "kind", "ref_id")

    def __init__(self, text_start: int, text_end: int, bbox: tuple,
                 kind: str, ref_id: str):
        self.text_start = text_start   # char offset in raw_text
        self.text_end   = text_end
        self.bbox       = bbox         # (x, y, w, h) page-absolute px
        self.kind       = kind         # "word" | "gap"
        self.ref_id     = ref_id       # gap_id or word index str


# ---------------------------------------------------------------------------
# LinkingEngine
# ---------------------------------------------------------------------------

class LinkingEngine:
    """Built once per page load. Immutable after construction."""

    def __init__(self, doc: PageDocument):
        self._doc = doc
        self._entries: list[BboxEntry] = []
        self._build_index()

    def _build_index(self):
        """Build sorted (by text_start) index from words + gaps."""
        entries = []

        # --- Word entries from ConfidenceRecord data ---
        if self._doc.word_bboxes:
            # Align words against the raw_text by simple sequential matching.
            # This is approximate but good enough for highlighting.
            entries.extend(self._index_words())

        # --- Gap entries from gap imgbbox fields ---
        for gap in self._doc.gaps:
            if gap.imgbbox and gap.imgbbox != "0,0,0,0":
                from gap_utils import parse_bbox
                x, y, w, h = parse_bbox(gap.imgbbox)
                if w > 0 and h > 0:
                    entries.append(BboxEntry(
                        text_start=gap.start,
                        text_end=gap.end,
                        bbox=(x, y, w, h),
                        kind="gap",
                        ref_id=gap.gap_id,
                    ))

        # Sort by text position for binary search
        entries.sort(key=lambda e: e.text_start)
        self._entries = entries

    def _index_words(self) -> list:
        """Map each WordBbox to an approximate char offset in raw_text."""
        entries = []
        text = self._doc.raw_text
        search_start = 0

        for i, wb in enumerate(self._doc.word_bboxes):
            if not wb.text or len(wb.text) < 1:
                continue
            # Find the word in text starting from last match position
            idx = text.find(wb.text, search_start)
            if idx == -1:
                # Try case-insensitive or skip
                lo = wb.text.lower()
                idx = text.lower().find(lo, search_start)
            if idx == -1:
                continue
            entries.append(BboxEntry(
                text_start=idx,
                text_end=idx + len(wb.text),
                bbox=(wb.x, wb.y, wb.w, wb.h),
                kind="word",
                ref_id=str(i),
            ))
            search_start = idx  # allow overlapping (text may repeat)
        return entries

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cursor_to_bbox(self, cursor_offset: int) -> Optional[BboxEntry]:
        """Return the BboxEntry whose text range contains cursor_offset."""
        # Linear scan — fast enough for typical page sizes (~few thousand entries)
        for e in self._entries:
            if e.text_start <= cursor_offset < e.text_end:
                return e
        return None

    def image_click_to_entry(self, px: int, py: int) -> Optional[BboxEntry]:
        """Return the BboxEntry whose bbox contains image point (px, py)."""
        # Check gaps first (priority)
        for e in self._entries:
            if e.kind == "gap":
                x, y, w, h = e.bbox
                if x <= px <= x + w and y <= py <= y + h:
                    return e
        # Then words
        for e in self._entries:
            if e.kind == "word":
                x, y, w, h = e.bbox
                if x <= px <= x + w and y <= py <= y + h:
                    return e
        return None

    def nearest_entry_to_image_point(self, px: int, py: int,
                                     max_dist: int = 50) -> Optional[BboxEntry]:
        """Return the entry whose bbox center is nearest to (px, py)."""
        best = None
        best_dist = float("inf")
        for e in self._entries:
            x, y, w, h = e.bbox
            cx, cy = x + w // 2, y + h // 2
            d = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best = e
        if best_dist <= max_dist:
            return best
        return None

    def gap_entry(self, gap_id: str) -> Optional[BboxEntry]:
        """Find a gap entry by its gap_id."""
        for e in self._entries:
            if e.kind == "gap" and e.ref_id == gap_id:
                return e
        return None

    def all_gap_entries(self) -> list:
        return [e for e in self._entries if e.kind == "gap"]

    def all_word_entries(self) -> list:
        return [e for e in self._entries if e.kind == "word"]
