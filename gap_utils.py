"""
gap_utils.py — Shared gap-tag parsing utilities.

Extracted from unt_ocr_correct.py so both the OCR pipeline and the GUI
can import without pulling in the entire correction script.

Gap tag format:
  {{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX"
         | status=STATUS | fragments="..." | region_ocr="..."
         | split_parent="x,y,w,h" [guess text] }}

All coordinates are page-absolute pixels (x, y = top-left, w, h = size).
"""

import re

# ---------------------------------------------------------------------------
# Core regex — matches the gap tag in all its variants.
# Groups:
#   1  est           (int, required)
#   2  imgbbox       (str "x,y,w,h", required)
#   3  cnf           (str float, optional)
#   4  status        (str, optional)
#   5  fragments     (str, optional)
#   6  region_ocr    (str, optional)
#   7  split_parent  (str "x,y,w,h", optional) — set when gap was split by GUI
#   8  guess         (str, required — content of [...])
# ---------------------------------------------------------------------------
GAP_RE = re.compile(
    r'\{\{\s*gap\s*\|'
    r'\s*est=(\d+)'
    r'\s*\|\s*imgbbox="([^"]*)"'
    r'(?:\s*\|\s*cnf="([^"]*)")?'
    r'(?:\s*\|\s*status=(\S+))?'
    r'(?:\s*\|\s*fragments="([^"]*)")?'
    r'(?:\s*\|\s*region_ocr="([^"]*)")?'
    r'(?:\s*\|\s*split_parent="([^"]*)")?'
    r'\s*\[([^\]]*)\]'
    r'\s*\}\}')


def parse_gaps(text: str) -> list:
    """Extract all gap tags from text with their positions and parsed fields.

    Returns list of dicts, each with:
        match, start, end, est, imgbbox, cnf, status,
        fragments, region_ocr, split_parent, guess, full_tag
    """
    gaps = []
    for m in GAP_RE.finditer(text):
        gaps.append({
            "match":        m,
            "start":        m.start(),
            "end":          m.end(),
            "est":          int(m.group(1)),
            "imgbbox":      m.group(2),
            "cnf":          float(m.group(3)) if m.group(3) else 0.0,
            "status":       m.group(4) or "",
            "fragments":    m.group(5) or "",
            "region_ocr":   m.group(6) or "",
            "split_parent": m.group(7) or "",
            "guess":        m.group(8),
            "full_tag":     m.group(0),
        })
    return gaps


def build_gap_tag(est: int, imgbbox: str, cnf: float = 0.0,
                  status: str = "", fragments: str = "",
                  region_ocr: str = "", split_parent: str = "",
                  guess: str = "") -> str:
    """Serialize a gap dict back to a tag string."""
    parts = [f"est={est}", f'imgbbox="{imgbbox}"']
    if cnf:
        parts.append(f'cnf="{cnf:.2f}"')
    if status:
        parts.append(f"status={status}")
    if fragments:
        parts.append(f'fragments="{fragments}"')
    if region_ocr:
        parts.append(f'region_ocr="{region_ocr}"')
    if split_parent:
        parts.append(f'split_parent="{split_parent}"')
    inner = " | ".join(parts)
    return "{{ gap | " + inner + " [" + guess + "] }}"


def get_context(text: str, start: int, end: int, chars: int = 200) -> str:
    """Get ~chars characters of context around a gap position."""
    before = text[max(0, start - chars):start].strip()
    after  = text[end:end + chars].strip()
    return f"...{before} [GAP] {after}..."


def parse_bbox(bbox_str: str) -> tuple:
    """Parse 'x,y,w,h' string into (x, y, w, h) ints. Returns (0,0,0,0) on error."""
    parts = bbox_str.split(",")
    if len(parts) == 4:
        try:
            return tuple(int(p.strip()) for p in parts)
        except ValueError:
            pass
    return (0, 0, 0, 0)


def bbox_to_str(bbox: tuple) -> str:
    """Convert (x, y, w, h) tuple to 'x,y,w,h' string."""
    return ",".join(str(v) for v in bbox)


def merge_bboxes(bboxes: list, padding: int = 50) -> tuple:
    """Merge a list of (x,y,w,h) bboxes into a single bounding box."""
    if not bboxes:
        return (0, 0, 0, 0)
    x_min = min(b[0] for b in bboxes) - padding
    y_min = min(b[1] for b in bboxes) - padding
    x_max = max(b[0] + b[2] for b in bboxes) + padding
    y_max = max(b[1] + b[3] for b in bboxes) + padding
    return (max(0, x_min), max(0, y_min),
            x_max - max(0, x_min), y_max - max(0, y_min))


def split_bbox_horizontal(bbox: tuple, split_x: int) -> tuple:
    """Split (x,y,w,h) into two sub-bboxes at an x coordinate.

    split_x is in page-absolute pixels. Returns (left_bbox, right_bbox).
    Each sub-bbox may have w=0 if split_x is outside the original.
    """
    x, y, w, h = bbox
    left_w  = max(0, split_x - x)
    right_x = x + left_w
    right_w = max(0, x + w - right_x)
    return (x, y, left_w, h), (right_x, y, right_w, h)


def group_gaps_by_bbox(gaps: list, proximity_px: int = 100) -> list:
    """Group gaps whose bboxes are within proximity_px vertically.
    Returns list of lists of gaps, sorted by y position."""
    if not gaps:
        return []
    sorted_gaps = sorted(gaps, key=lambda g: parse_bbox(g["imgbbox"])[1])
    groups = []
    current_group = [sorted_gaps[0]]
    current_bottom = (lambda b: b[1] + b[3])(parse_bbox(sorted_gaps[0]["imgbbox"]))

    for gap in sorted_gaps[1:]:
        bbox = parse_bbox(gap["imgbbox"])
        if bbox[1] <= current_bottom + proximity_px:
            current_group.append(gap)
            current_bottom = max(current_bottom, bbox[1] + bbox[3])
        else:
            groups.append(current_group)
            current_group = [gap]
            current_bottom = bbox[1] + bbox[3]

    groups.append(current_group)
    return groups
