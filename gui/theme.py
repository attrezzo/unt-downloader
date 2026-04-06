"""
gui/theme.py — Color constants for the OCR correction GUI.

All colors are (R, G, B, A) tuples with values 0-255.
DPG expects colors in this format for draw commands and widget styling.
"""

# ---------------------------------------------------------------------------
# Gap status colors
# ---------------------------------------------------------------------------
GAP_PENDING   = (255, 160,  40, 200)   # orange
GAP_CORRECTED = ( 60, 200,  60, 200)   # green
GAP_ACCEPTED  = (100, 200, 100, 160)   # light green
GAP_MISSING   = (200,  60,  60, 200)   # red
GAP_PARTIAL   = (200, 160,  60, 200)   # amber (split)

GAP_STATUS_COLORS = {
    "":                GAP_PENDING,
    "pending":         GAP_PENDING,
    "auto-resolved":   (160, 200, 255, 180),   # light blue
    "human-accepted":  GAP_ACCEPTED,
    "human-corrected": GAP_CORRECTED,
    "human-missing":   GAP_MISSING,
    "human-partial":   GAP_PARTIAL,
}

# ---------------------------------------------------------------------------
# Confidence level colors (for word-level highlighting)
# ---------------------------------------------------------------------------
CONF_HIGH   = ( 60, 200,  60, 120)   # ≥ 70 — green
CONF_MEDIUM = (255, 200,  40, 120)   # 40-69 — yellow
CONF_LOW    = (255, 100,  40, 120)   # < 40 — orange-red

def conf_color(confidence: int) -> tuple:
    if confidence >= 70:
        return CONF_HIGH
    if confidence >= 40:
        return CONF_MEDIUM
    return CONF_LOW

# ---------------------------------------------------------------------------
# Selected word / active highlight
# ---------------------------------------------------------------------------
SELECTION_HIGHLIGHT  = ( 80, 160, 255, 200)   # blue — hovered in image
ACTIVE_GAP_OUTLINE   = (255, 220,   0, 255)   # bright yellow — selected gap border

# ---------------------------------------------------------------------------
# Region type overlay colors (semi-transparent fills)
# ---------------------------------------------------------------------------
REGION_COLORS = {
    "column":        (100, 160, 255,  60),
    "article":       (100, 220, 100,  60),
    "advertisement": (255, 200,  60,  60),
    "picture":       (200, 100, 200,  60),
    "masthead":      (255, 120,  60,  60),
    "notice":        ( 60, 200, 200,  60),
    "footer":        (160, 160, 160,  60),
    "other":         (200, 200, 200,  60),
}

REGION_BORDER_COLORS = {
    "column":        (100, 160, 255, 200),
    "article":       (100, 220, 100, 200),
    "advertisement": (255, 200,  60, 200),
    "picture":       (200, 100, 200, 200),
    "masthead":      (255, 120,  60, 200),
    "notice":        ( 60, 200, 200, 200),
    "footer":        (160, 160, 160, 200),
    "other":         (200, 200, 200, 200),
}

# ---------------------------------------------------------------------------
# UI chrome
# ---------------------------------------------------------------------------
PANEL_BG         = ( 28,  28,  30, 255)
TOOLBAR_BG       = ( 38,  38,  42, 255)
TEXT_DEFAULT     = (230, 230, 230, 255)
TEXT_MUTED       = (140, 140, 140, 255)
TEXT_WARNING     = (255, 200,  60, 255)
TEXT_ERROR       = (255,  80,  80, 255)
DIVIDER          = ( 70,  70,  75, 255)
BUTTON_PRIMARY   = ( 60, 120, 220, 255)
BUTTON_DANGER    = (200,  60,  60, 255)
BUTTON_SUCCESS   = ( 60, 180,  60, 255)
