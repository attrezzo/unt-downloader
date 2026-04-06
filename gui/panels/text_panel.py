"""
gui/panels/text_panel.py — Right panel: OCR text display + gap table.

Two-part layout:
  1. Upper: read-only multiline text showing the raw ai_ocr markdown
             with gap tags displayed in a simplified human-readable form.
  2. Lower: scrollable gap table with columns:
             #, Status, Est. Chars, Confidence, Guess
             Each row is clickable → selects gap → opens gap inspector.

DPG limitation: dpg.add_input_text(multiline=True) is a plain text widget
with no inline widget or per-character colour support. Gap tags are therefore
displayed as simplified text markers like [GAP #3 — 12ch — 45%].
"""

from __future__ import annotations
import re
from typing import Optional

import dearpygui.dearpygui as dpg

from ..theme import GAP_STATUS_COLORS, TEXT_DEFAULT, TEXT_MUTED
from ..models.gap import GapData, STATUS_PENDING
from .. import state as state_mod


_GAP_PLACEHOLDER_RE = re.compile(
    r'\{\{\s*gap\s*\|[^}]+\}\}', re.DOTALL)


def _render_text_for_display(raw: str, gaps: list) -> str:
    """Replace gap tags with short human-readable placeholders."""
    result = raw
    # Replace in reverse order to preserve offsets
    for gap in reversed(gaps):
        placeholder = (f"[GAP {gap.gap_id} | "
                       f"{gap.display_status.upper()} | "
                       f"est={gap.est}ch | "
                       f"cnf={gap.cnf:.2f} | "
                       f"{gap.display_text[:20] or '?'}]")
        result = result[:gap.start] + placeholder + result[gap.end:]
    return result


class TextPanel:
    """Right-side text display and gap table."""

    _WINDOW_TAG  = "text_panel_window"
    _TEXT_TAG    = "text_panel_text"
    _TABLE_TAG   = "text_panel_gap_table"
    _FILTER_TAG  = "text_panel_filter"

    def __init__(self, app_state: state_mod.AppState,
                 width: int = 700, height: int = 900):
        self.state  = app_state
        self.width  = width
        self.height = height

        self._on_gap_select_cb: Optional[callable] = None

        self.state.on(state_mod.EVT_PAGE_CHANGED,   self._on_page_changed)
        self.state.on(state_mod.EVT_GAP_EDITED,     self._refresh_table)
        self.state.on(state_mod.EVT_GAP_SELECTED,   self._on_gap_selected)
        self.state.on(state_mod.EVT_COMPLETION_CHANGED, self._refresh_text)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, parent):
        with dpg.child_window(tag=self._WINDOW_TAG, parent=parent,
                              width=self.width, height=self.height):
            # --- Text display (upper ~55% of panel) ---
            text_h = int(self.height * 0.50)
            dpg.add_text("OCR Text", color=TEXT_MUTED)
            dpg.add_input_text(
                tag=self._TEXT_TAG,
                multiline=True,
                readonly=True,
                width=self.width - 20,
                height=text_h,
                default_value="(Load a collection to begin)",
            )
            dpg.add_separator()

            # --- Gap table (lower ~45%) ---
            dpg.add_text("Gaps", color=TEXT_MUTED)

            # Filter bar
            with dpg.group(horizontal=True):
                dpg.add_text("Filter:", color=TEXT_MUTED)
                dpg.add_combo(
                    tag=self._FILTER_TAG,
                    items=["all", "pending", "corrected", "missing", "accepted"],
                    default_value="all",
                    width=130,
                    callback=self._refresh_table,
                )

            # Gap table
            gap_table_h = self.height - text_h - 90
            with dpg.child_window(width=self.width - 10, height=gap_table_h):
                with dpg.table(tag=self._TABLE_TAG,
                               header_row=True,
                               borders_innerH=True,
                               borders_outerH=True,
                               borders_outerV=True,
                               scrollY=True,
                               policy=dpg.mvTable_SizingFixedFit):
                    dpg.add_table_column(label="#",       width_fixed=True, init_width_or_weight=45)
                    dpg.add_table_column(label="Status",  width_fixed=True, init_width_or_weight=110)
                    dpg.add_table_column(label="Est",     width_fixed=True, init_width_or_weight=45)
                    dpg.add_table_column(label="Cnf",     width_fixed=True, init_width_or_weight=50)
                    dpg.add_table_column(label="Guess / Correction", width_stretch=True)

    # ------------------------------------------------------------------
    # Refresh helpers
    # ------------------------------------------------------------------

    def _on_page_changed(self, doc, page, **_):
        self._refresh_text()
        self._refresh_table()

    def _refresh_text(self, **_):
        if not dpg.does_item_exist(self._TEXT_TAG):
            return
        doc = self.state.page_doc
        if not doc:
            dpg.set_value(self._TEXT_TAG, "(No page loaded)")
            return
        display = _render_text_for_display(doc.raw_text, doc.gaps)
        dpg.set_value(self._TEXT_TAG, display)

    def _refresh_table(self, **_):
        if not dpg.does_item_exist(self._TABLE_TAG):
            return
        # Clear existing rows
        dpg.delete_item(self._TABLE_TAG, children_only=True, slot=1)
        dpg.add_table_column(label="#",       width_fixed=True, init_width_or_weight=45,
                             parent=self._TABLE_TAG)
        dpg.add_table_column(label="Status",  width_fixed=True, init_width_or_weight=110,
                             parent=self._TABLE_TAG)
        dpg.add_table_column(label="Est",     width_fixed=True, init_width_or_weight=45,
                             parent=self._TABLE_TAG)
        dpg.add_table_column(label="Cnf",     width_fixed=True, init_width_or_weight=50,
                             parent=self._TABLE_TAG)
        dpg.add_table_column(label="Guess / Correction", width_stretch=True,
                             parent=self._TABLE_TAG)

        doc = self.state.page_doc
        if not doc:
            return

        filt = "all"
        if dpg.does_item_exist(self._FILTER_TAG):
            filt = dpg.get_value(self._FILTER_TAG)

        for gap in doc.gaps:
            if filt != "all" and gap.display_status != filt:
                continue
            self._add_gap_row(gap)

    def _add_gap_row(self, gap: GapData):
        color = GAP_STATUS_COLORS.get(gap.status, GAP_STATUS_COLORS[""])
        with dpg.table_row(parent=self._TABLE_TAG):
            # # column — clickable selectable
            gap_id = gap.gap_id
            with dpg.table_cell():
                dpg.add_selectable(
                    label=gap_id.replace("gap_", ""),
                    span_columns=False,
                    callback=lambda s, a, u: self.state.select_gap(u),
                    user_data=gap_id,
                )
            # Status
            with dpg.table_cell():
                dpg.add_text(gap.display_status, color=color)
            # Est chars
            with dpg.table_cell():
                dpg.add_text(str(gap.est))
            # Confidence
            with dpg.table_cell():
                cnf_pct = f"{gap.cnf*100:.0f}%"
                dpg.add_text(cnf_pct)
            # Guess / correction
            with dpg.table_cell():
                txt = gap.display_text[:60] or "—"
                dpg.add_text(txt, color=color if gap.is_human_edited else TEXT_DEFAULT)

    def _on_gap_selected(self, gap_id, **_):
        """Scroll text display to show the selected gap and highlight its row."""
        # Refresh table to re-render selection state
        self._refresh_table()

    def set_gap_select_callback(self, cb: callable):
        """Register callback invoked when user clicks a gap row."""
        self._on_gap_select_cb = cb
