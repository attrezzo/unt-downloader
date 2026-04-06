"""
gui/models/char_measure.py — Character space measurement and gap estimation.

Uses per-region char_width_px overrides (from the document map) or falls
back to the issue StyleSignature defaults. Provides:
  - estimate_gap_chars(): pixel width → estimated character count
  - CharMeasurement: interactive ruler state (two-click measurement)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CharDefaults:
    """Character size defaults sourced from a StyleSignature or user entry."""
    char_width_px:  float = 18.0
    char_height_px: float = 24.0
    source: str = "manual"   # "style_signature" | "manual" | "measured"

    @classmethod
    def from_style_signature(cls, sig: dict) -> CharDefaults:
        """Build from an ocr_pipeline StyleSignature dict."""
        h = sig.get("median_char_height", 24.0) or 24.0
        # Estimate width as ~60% of height (typical for German Fraktur)
        w = sig.get("median_stroke_width", h * 0.6) or h * 0.6
        # stroke_width is ink width, not glyph advance — scale up
        w = max(w * 2.5, h * 0.5)
        return cls(char_width_px=round(w, 1),
                   char_height_px=round(h, 1),
                   source="style_signature")


def estimate_gap_chars(gap_width_px: int, char_width_px: float) -> int:
    """Return estimated character count for a gap of given pixel width."""
    if char_width_px <= 0:
        return 0
    return max(1, round(gap_width_px / char_width_px))


def estimate_gap_width_px(char_count: int, char_width_px: float) -> int:
    """Inverse: how wide (px) would N chars be?"""
    return round(char_count * char_width_px)


@dataclass
class CharMeasurement:
    """Interactive two-click ruler state.

    Usage:
      m = CharMeasurement()
      m.click(x1, y1)      # first anchor
      m.click(x2, y2)      # second anchor
      m.char_count = 10    # user tells us how many chars span this distance
      result = m.char_width_px   # computed width per character
    """
    point_a: Optional[tuple] = None   # (x, y) image-space
    point_b: Optional[tuple] = None
    char_count: int = 0               # user-supplied denominator

    def click(self, x: int, y: int):
        if self.point_a is None:
            self.point_a = (x, y)
        else:
            self.point_b = (x, y)

    def reset(self):
        self.point_a = None
        self.point_b = None
        self.char_count = 0

    @property
    def is_ready(self) -> bool:
        """True when both points are set."""
        return self.point_a is not None and self.point_b is not None

    @property
    def pixel_distance(self) -> float:
        if not self.is_ready:
            return 0.0
        dx = self.point_b[0] - self.point_a[0]
        dy = self.point_b[1] - self.point_a[1]
        return (dx * dx + dy * dy) ** 0.5

    @property
    def char_width_px(self) -> Optional[float]:
        """Computed character width. None if char_count not yet supplied."""
        if not self.is_ready or self.char_count <= 0:
            return None
        return round(self.pixel_distance / self.char_count, 2)

    def __str__(self) -> str:
        if not self.is_ready:
            return "Click two points on the image to measure character width"
        d = self.pixel_distance
        if self.char_count > 0:
            cw = self.char_width_px
            return f"{d:.0f}px / {self.char_count} chars = {cw:.1f}px/char"
        return f"Distance: {d:.0f}px — enter character count"
