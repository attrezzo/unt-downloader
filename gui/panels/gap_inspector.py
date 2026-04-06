"""
gui/panels/gap_inspector.py — Gap detail and editing panel.

Shown in a child window on the right or as a modal popup when a gap is selected.
Displays:
  - Cropped image of the gap region from the page scan
  - Gap metadata: est chars, confidence, fragments, region_ocr, status
  - Action buttons: Accept, Correct (text input), Missing, Split
  - Split mode: shows gap image with draggable vertical divider
"""

from __future__ import annotations
import io
from typing import Optional

import dearpygui.dearpygui as dpg

from ..theme import (GAP_STATUS_COLORS, BUTTON_PRIMARY, BUTTON_DANGER,
                     BUTTON_SUCCESS, TEXT_DEFAULT, TEXT_MUTED, TEXT_WARNING)
from ..models.gap import GapData, STATUS_CORRECTED, STATUS_MISSING
from .. import state as state_mod


_CROP_TEXTURE_TAG = "gap_inspector_crop"
_PANEL_TAG        = "gap_inspector_panel"
_MODAL_TAG        = "gap_inspector_modal"

# Max size for the cropped gap preview
_CROP_MAX_W = 500
_CROP_MAX_H = 120


class GapInspector:
    """Gap editing panel. Builds either as a docked child window or modal."""

    def __init__(self, app_state: state_mod.AppState,
                 width: int = 660, height: int = 340):
        self.state  = app_state
        self.width  = width
        self.height = height

        self._current_gap: Optional[GapData] = None
        self._correction_input_tag: Optional[str] = None
        self._split_x: Optional[int] = None
        self._crop_texture_registered = False

        self.state.on(state_mod.EVT_GAP_SELECTED, self._on_gap_selected)
        self.state.on(state_mod.EVT_PAGE_CHANGED,  self._on_page_changed)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, parent):
        with dpg.child_window(tag=_PANEL_TAG, parent=parent,
                              width=self.width, height=self.height,
                              border=True):
            dpg.add_text("Gap Inspector", color=TEXT_MUTED)
            dpg.add_separator()
            dpg.add_text("(Select a gap from the table or image)",
                         tag="gap_inspector_placeholder",
                         color=TEXT_MUTED)

    # ------------------------------------------------------------------
    # Update when a gap is selected
    # ------------------------------------------------------------------

    def _on_gap_selected(self, gap_id, **_):
        if not dpg.does_item_exist(_PANEL_TAG):
            return
        dpg.delete_item(_PANEL_TAG, children_only=True)

        if not gap_id or not self.state.page_doc:
            dpg.add_text("(Select a gap)", color=TEXT_MUTED, parent=_PANEL_TAG)
            return

        gap = self._find_gap(gap_id)
        if not gap:
            return
        self._current_gap = gap
        self._build_content(gap)

    def _on_page_changed(self, **_):
        if dpg.does_item_exist(_PANEL_TAG):
            dpg.delete_item(_PANEL_TAG, children_only=True)
            dpg.add_text("(Select a gap)", color=TEXT_MUTED, parent=_PANEL_TAG)

    def _find_gap(self, gap_id: str) -> Optional[GapData]:
        if not self.state.page_doc:
            return None
        for g in self.state.page_doc.gaps:
            if g.gap_id == gap_id:
                return g
        return None

    # ------------------------------------------------------------------
    # Content builder
    # ------------------------------------------------------------------

    def _build_content(self, gap: GapData):
        p = _PANEL_TAG

        # --- Header ---
        dpg.add_text(f"Gap Inspector — {gap.gap_id}", parent=p)
        status_color = GAP_STATUS_COLORS.get(gap.status, GAP_STATUS_COLORS[""])
        dpg.add_text(f"Status: {gap.display_status.upper()}",
                     color=status_color, parent=p)
        dpg.add_separator(parent=p)

        # --- Metadata row ---
        with dpg.group(horizontal=True, parent=p):
            dpg.add_text(f"Est: {gap.est} chars", color=TEXT_MUTED)
            dpg.add_text("  |  ", color=TEXT_MUTED)
            dpg.add_text(f"Confidence: {gap.cnf*100:.0f}%", color=TEXT_MUTED)
            dpg.add_text("  |  ", color=TEXT_MUTED)
            dpg.add_text(f"BBox: {gap.imgbbox}", color=TEXT_MUTED)

        if gap.fragments:
            dpg.add_text(f"Fragments: {gap.fragments}", color=TEXT_WARNING, parent=p)
        if gap.region_ocr:
            dpg.add_text(f"OCR ref: {gap.region_ocr[:80]}", color=TEXT_MUTED, parent=p)

        dpg.add_separator(parent=p)

        # --- Cropped image preview ---
        self._build_crop_preview(gap, p)
        dpg.add_separator(parent=p)

        # --- Current guess ---
        dpg.add_text("Machine guess:", color=TEXT_MUTED, parent=p)
        dpg.add_input_text(
            default_value=gap.guess or "",
            readonly=True,
            width=self.width - 30,
            parent=p,
        )
        dpg.add_separator(parent=p)

        # --- Correction input ---
        dpg.add_text("Your correction:", color=TEXT_DEFAULT, parent=p)
        corr_tag = f"gap_correction_{gap.gap_id}"
        self._correction_input_tag = corr_tag
        dpg.add_input_text(
            tag=corr_tag,
            default_value=gap.user_correction or gap.guess or "",
            hint="Type corrected text here…",
            width=self.width - 30,
            parent=p,
            on_enter=True,
            callback=self._on_correction_enter,
        )
        dpg.add_separator(parent=p)

        # --- Action buttons ---
        with dpg.group(horizontal=True, parent=p):
            dpg.add_button(
                label="Accept Guess",
                callback=self._on_accept,
                user_data=gap.gap_id,
            )
            dpg.add_button(
                label="Save Correction",
                callback=self._on_correct,
                user_data=gap.gap_id,
            )
            dpg.add_button(
                label="Mark Missing",
                callback=self._on_missing,
                user_data=gap.gap_id,
            )
            dpg.add_button(
                label="Split Gap",
                callback=self._on_split_start,
                user_data=gap.gap_id,
            )

        # --- Split controls (hidden initially) ---
        split_grp_tag = f"gap_split_grp_{gap.gap_id}"
        with dpg.group(tag=split_grp_tag, parent=p, show=False):
            dpg.add_separator()
            dpg.add_text("Split: set the x-pixel position dividing left (recoverable) "
                         "from right (missing).", color=TEXT_MUTED)
            with dpg.group(horizontal=True):
                from gap_utils import parse_bbox
                x, y, w, h = parse_bbox(gap.imgbbox)
                mid = x + w // 2
                split_slider = f"gap_split_slider_{gap.gap_id}"
                dpg.add_slider_int(
                    tag=split_slider,
                    label="Split X (page px)",
                    default_value=mid,
                    min_value=x,
                    max_value=x + w,
                    width=300,
                )
                dpg.add_button(
                    label="Apply Split",
                    callback=self._on_split_apply,
                    user_data=(gap.gap_id, split_slider),
                )

        self._split_grp_tag = split_grp_tag

    def _build_crop_preview(self, gap: GapData, parent):
        """Load cropped gap image from page JPEG and display as texture."""
        if not self.state.page_doc or not self.state.page_doc.image_path:
            dpg.add_text("(No image available)", color=TEXT_MUTED, parent=parent)
            return
        try:
            from PIL import Image
            from gap_utils import parse_bbox
            img = Image.open(self.state.page_doc.image_path).convert("RGBA")
            x, y, w, h = parse_bbox(gap.imgbbox)
            if w <= 0 or h <= 0:
                dpg.add_text("(Gap has no bbox)", color=TEXT_MUTED, parent=parent)
                return

            # Add padding
            pad = 10
            iw, ih = img.size
            cx0, cy0 = max(0, x - pad), max(0, y - pad)
            cx1, cy1 = min(iw, x + w + pad), min(ih, y + h + pad)
            crop = img.crop((cx0, cy0, cx1, cy1))

            # Scale to fit preview area
            cw, ch = crop.size
            scale = min(_CROP_MAX_W / max(cw, 1), _CROP_MAX_H / max(ch, 1), 1.0)
            dw, dh = max(1, int(cw * scale)), max(1, int(ch * scale))
            crop = crop.resize((dw, dh), Image.LANCZOS)

            flat = []
            for r, g_c, b, a in crop.getdata():
                flat += [r / 255, g_c / 255, b / 255, a / 255]

            tag = f"gap_crop_{gap.gap_id}"
            if dpg.does_item_exist(tag):
                dpg.delete_item(tag)
            with dpg.texture_registry():
                dpg.add_static_texture(dw, dh, flat, tag=tag)

            dpg.add_image(tag, width=dw, height=dh, parent=parent)
        except Exception as e:
            dpg.add_text(f"(Crop error: {e})", color=TEXT_MUTED, parent=parent)

    # ------------------------------------------------------------------
    # Button callbacks
    # ------------------------------------------------------------------

    def _on_correction_enter(self, sender, app_data, user_data):
        self._on_correct(sender, app_data, self._current_gap.gap_id
                         if self._current_gap else None)

    def _on_accept(self, sender, app_data, gap_id):
        self.state.edit_gap(gap_id, "accept")
        self._refresh()

    def _on_correct(self, sender, app_data, gap_id):
        if not gap_id or not self._correction_input_tag:
            return
        text = dpg.get_value(self._correction_input_tag)
        self.state.edit_gap(gap_id, "correct", text=text)
        self._refresh()

    def _on_missing(self, sender, app_data, gap_id):
        self.state.edit_gap(gap_id, "missing")
        self._refresh()

    def _on_split_start(self, sender, app_data, gap_id):
        if hasattr(self, "_split_grp_tag") and dpg.does_item_exist(self._split_grp_tag):
            current = dpg.get_item_configuration(self._split_grp_tag)["show"]
            dpg.configure_item(self._split_grp_tag, show=not current)

    def _on_split_apply(self, sender, app_data, user_data):
        gap_id, slider_tag = user_data
        split_x = dpg.get_value(slider_tag)
        self.state.edit_gap(gap_id, "split", split_x=split_x)
        self._refresh()

    def _refresh(self):
        if self._current_gap:
            self._on_gap_selected(gap_id=self._current_gap.gap_id)
