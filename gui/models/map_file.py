"""
gui/models/map_file.py — Document region map.

One map file per page: {collection}/gui/maps/{ark_id}_page{NN}_map.json

Regions describe physical layout zones on the document image:
columns, articles, advertisements, pictures, mastheads, notices, footers.
Each region stores its geometry (rect or polygon) in page-absolute pixels
and optionally a per-region character size override.
"""

from __future__ import annotations
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Region type catalogue
# ---------------------------------------------------------------------------
REGION_TYPES = [
    "column",
    "article",
    "advertisement",
    "picture",
    "masthead",
    "notice",
    "footer",
    "other",
]

# Display colors per type (RGBA 0-255) — mirrored in theme.py for DPG
REGION_TYPE_COLORS = {
    "column":        (100, 160, 255, 80),
    "article":       (100, 220, 100, 80),
    "advertisement": (255, 200,  60, 80),
    "picture":       (200, 100, 200, 80),
    "masthead":      (255, 120,  60, 80),
    "notice":        (60,  200, 200, 80),
    "footer":        (160, 160, 160, 80),
    "other":         (200, 200, 200, 80),
}


@dataclass
class Geometry:
    """A region's shape — either a simple rectangle or a polygon."""
    kind: str = "rect"   # "rect" | "polygon"
    # For kind="rect":
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    # For kind="polygon":
    points: list = field(default_factory=list)  # list of [x, y]

    def to_dict(self) -> dict:
        if self.kind == "rect":
            return {"kind": "rect", "x": self.x, "y": self.y,
                    "w": self.w, "h": self.h}
        return {"kind": "polygon", "points": self.points}

    @classmethod
    def from_dict(cls, d: dict) -> Geometry:
        if d.get("kind") == "polygon":
            return cls(kind="polygon", points=d.get("points", []))
        return cls(kind="rect",
                   x=d.get("x", 0), y=d.get("y", 0),
                   w=d.get("w", 0), h=d.get("h", 0))

    def bbox(self) -> tuple:
        """Return (x, y, w, h) bounding box regardless of kind."""
        if self.kind == "rect":
            return (self.x, self.y, self.w, self.h)
        if not self.points:
            return (0, 0, 0, 0)
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        x0, y0 = min(xs), min(ys)
        return (x0, y0, max(xs) - x0, max(ys) - y0)

    def contains_point(self, px: int, py: int) -> bool:
        """Return True if (px, py) is inside this geometry."""
        if self.kind == "rect":
            return (self.x <= px <= self.x + self.w and
                    self.y <= py <= self.y + self.h)
        # Ray-casting for polygon
        n = len(self.points)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = self.points[i]
            xj, yj = self.points[j]
            if ((yi > py) != (yj > py) and
                    px < (xj - xi) * (py - yi) / ((yj - yi) or 1e-9) + xi):
                inside = not inside
            j = i
        return inside


@dataclass
class Region:
    """A named layout region on a document page."""
    id:            str
    type:          str            # one of REGION_TYPES
    label:         str
    geometry:      Geometry
    char_width_px:  Optional[float] = None  # user override; None = use default
    char_height_px: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "id":            self.id,
            "type":          self.type,
            "label":         self.label,
            "geometry":      self.geometry.to_dict(),
            "char_width_px":  self.char_width_px,
            "char_height_px": self.char_height_px,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Region:
        return cls(
            id=d.get("id", str(uuid.uuid4())[:8]),
            type=d.get("type", "other"),
            label=d.get("label", ""),
            geometry=Geometry.from_dict(d.get("geometry", {})),
            char_width_px=d.get("char_width_px"),
            char_height_px=d.get("char_height_px"),
        )

    @property
    def color(self) -> tuple:
        return REGION_TYPE_COLORS.get(self.type, REGION_TYPE_COLORS["other"])


class DocumentMap:
    """All user-defined regions for one page."""

    def __init__(self, ark_id: str, page_num: int,
                 image_width: int = 0, image_height: int = 0):
        self.ark_id      = ark_id
        self.page_num    = page_num
        self.image_width  = image_width
        self.image_height = image_height
        self.regions: list[Region] = []
        self.char_defaults: dict = {}   # from style_signature

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def add_region(self, type_: str, label: str, geometry: Geometry,
                   char_width_px: Optional[float] = None,
                   char_height_px: Optional[float] = None) -> Region:
        r = Region(
            id=f"r{len(self.regions)+1:03d}",
            type=type_,
            label=label,
            geometry=geometry,
            char_width_px=char_width_px,
            char_height_px=char_height_px,
        )
        self.regions.append(r)
        return r

    def remove_region(self, region_id: str):
        self.regions = [r for r in self.regions if r.id != region_id]

    def get_region(self, region_id: str) -> Optional[Region]:
        for r in self.regions:
            if r.id == region_id:
                return r
        return None

    def region_at_point(self, px: int, py: int) -> Optional[Region]:
        """Return the topmost region containing (px, py), or None."""
        for r in reversed(self.regions):
            if r.geometry.contains_point(px, py):
                return r
        return None

    def char_width_at(self, px: int, py: int) -> Optional[float]:
        """Return char_width_px for the region at (px,py), or the page default."""
        r = self.region_at_point(px, py)
        if r and r.char_width_px is not None:
            return r.char_width_px
        return self.char_defaults.get("char_width_px")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version":      1,
            "ark_id":       self.ark_id,
            "page_num":     self.page_num,
            "image_width":  self.image_width,
            "image_height": self.image_height,
            "regions":      [r.to_dict() for r in self.regions],
            "char_defaults": self.char_defaults,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DocumentMap:
        dm = cls(
            ark_id=d.get("ark_id", ""),
            page_num=d.get("page_num", 0),
            image_width=d.get("image_width", 0),
            image_height=d.get("image_height", 0),
        )
        dm.regions = [Region.from_dict(r) for r in d.get("regions", [])]
        dm.char_defaults = d.get("char_defaults", {})
        return dm

    def save(self, collection_dir: Path):
        path = map_path(collection_dir, self.ark_id, self.page_num)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, collection_dir: Path,
             ark_id: str, page_num: int) -> DocumentMap:
        path = map_path(collection_dir, ark_id, page_num)
        if not path.exists():
            return cls(ark_id=ark_id, page_num=page_num)
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def map_path(collection_dir: Path, ark_id: str, page_num: int) -> Path:
    return (Path(collection_dir) / "gui" / "maps" /
            f"{ark_id}_page{page_num:02d}_map.json")
