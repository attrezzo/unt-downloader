"""
gui/models/gap.py — Gap data model for the correction GUI.

Wraps gap_utils parsing with GUI-specific state (user actions, split children).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import sys
from pathlib import Path

# Make gap_utils importable whether run from repo root or as a package
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from gap_utils import (parse_gaps as _parse_gaps, build_gap_tag,
                       parse_bbox, bbox_to_str, split_bbox_horizontal)

# ---------------------------------------------------------------------------
# Gap status values
# ---------------------------------------------------------------------------
# Pipeline statuses (written by automated passes)
STATUS_AUTO_RESOLVED = "auto-resolved"
# GUI statuses (written by human reviewer)
STATUS_PENDING         = ""               # untouched — shown as pending in UI
STATUS_ACCEPTED        = "human-accepted" # user accepted the machine guess
STATUS_CORRECTED       = "human-corrected"
STATUS_MISSING         = "human-missing"  # permanently unrecoverable
STATUS_PARTIAL         = "human-partial"  # gap was split; sub-gaps replace it

HUMAN_STATUSES = {STATUS_ACCEPTED, STATUS_CORRECTED, STATUS_MISSING, STATUS_PARTIAL}


@dataclass
class GapData:
    """One gap tag parsed from an ai_ocr page, with GUI editing state."""

    # --- From tag ---
    est:          int
    imgbbox:      str          # "x,y,w,h" page-absolute px
    cnf:          float        # 0.0-1.0
    status:       str          # see STATUS_* constants
    fragments:    str
    region_ocr:   str
    split_parent: str          # non-empty when this gap is a sub-gap
    guess:        str          # best machine prediction

    # --- Position in source text ---
    start: int = 0
    end:   int = 0

    # --- GUI state ---
    gap_id:          str            = ""    # e.g. "gap_003"
    user_correction: Optional[str]  = None  # text the user typed
    split_children:  list           = field(default_factory=list)  # list[GapData]
    original_status: str            = ""    # status before any user edit

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @property
    def bbox(self) -> tuple:
        return parse_bbox(self.imgbbox)

    @property
    def is_human_edited(self) -> bool:
        return self.status in HUMAN_STATUSES

    @property
    def display_status(self) -> str:
        if not self.status:
            return "pending"
        return self.status.replace("human-", "")

    @property
    def display_text(self) -> str:
        """Best text to show the user — correction trumps guess."""
        if self.user_correction is not None:
            return self.user_correction
        return self.guess or ""

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def accept(self):
        """Accept the machine guess as-is."""
        self.user_correction = self.guess
        self.status = STATUS_ACCEPTED

    def correct(self, text: str):
        """User provides explicit corrected text."""
        self.user_correction = text
        self.status = STATUS_CORRECTED

    def mark_missing(self, note: str = ""):
        """Mark gap as permanently unrecoverable."""
        self.user_correction = note or "[missing]"
        self.status = STATUS_MISSING

    def split(self, split_x_page: int) -> tuple:
        """Split this gap horizontally at a page-absolute x coordinate.

        Returns (left_child, right_child) as new GapData instances.
        The caller should replace this gap with the two children and
        set this gap's status to STATUS_PARTIAL.
        """
        bbox = self.bbox
        left_bbox, right_bbox = split_bbox_horizontal(bbox, split_x_page)

        def _child(bb: tuple, suffix: str) -> GapData:
            w = bb[2]
            # Estimate chars proportionally by width
            parent_w = bbox[2] or 1
            est_chars = max(1, round(self.est * w / parent_w))
            return GapData(
                est=est_chars,
                imgbbox=bbox_to_str(bb),
                cnf=0.0,
                status=STATUS_PENDING,
                fragments="",
                region_ocr="",
                split_parent=self.imgbbox,
                guess="",
                gap_id=self.gap_id + suffix,
                original_status="",
            )

        left  = _child(left_bbox,  "_L")
        right = _child(right_bbox, "_R")
        self.status = STATUS_PARTIAL
        self.split_children = [left, right]
        return left, right

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_tag(self) -> str:
        """Serialize back to a valid gap tag string."""
        effective_status = self.status
        effective_guess  = self.user_correction if self.user_correction is not None else self.guess
        return build_gap_tag(
            est=self.est,
            imgbbox=self.imgbbox,
            cnf=self.cnf,
            status=effective_status,
            fragments=self.fragments,
            region_ocr=self.region_ocr,
            split_parent=self.split_parent,
            guess=effective_guess or "",
        )

    def to_dict(self) -> dict:
        return {
            "gap_id":          self.gap_id,
            "est":             self.est,
            "imgbbox":         self.imgbbox,
            "cnf":             self.cnf,
            "status":          self.status,
            "fragments":       self.fragments,
            "region_ocr":      self.region_ocr,
            "split_parent":    self.split_parent,
            "guess":           self.guess,
            "user_correction": self.user_correction,
            "original_status": self.original_status,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def gaps_from_text(text: str) -> list:
    """Parse all gap tags in a text and return list[GapData]."""
    raw = _parse_gaps(text)
    result = []
    for i, r in enumerate(raw):
        gd = GapData(
            est=r["est"],
            imgbbox=r["imgbbox"],
            cnf=r["cnf"],
            status=r["status"],
            fragments=r["fragments"],
            region_ocr=r["region_ocr"],
            split_parent=r["split_parent"],
            guess=r["guess"],
            start=r["start"],
            end=r["end"],
            gap_id=f"gap_{i:03d}",
            original_status=r["status"],
        )
        result.append(gd)
    return result


def apply_gap_edits(source_text: str, gaps: list) -> str:
    """Rewrite source_text with edited gap tags in-place.

    gaps must be in the same order as they appear in source_text.
    Split gaps are expanded: the parent tag is replaced by child tags.
    """
    # Work backwards so offsets stay valid
    ordered = sorted(gaps, key=lambda g: g.start, reverse=True)
    out = source_text
    for g in ordered:
        if g.status == STATUS_PARTIAL and g.split_children:
            replacement = " ".join(c.to_tag() for c in g.split_children)
        else:
            replacement = g.to_tag()
        out = out[:g.start] + replacement + out[g.end:]
    return out
