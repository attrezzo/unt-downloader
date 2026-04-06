"""
gui/panels/status_bar.py — Bottom status bar: gap stats, confidence, zoom.
"""

from __future__ import annotations

import dearpygui.dearpygui as dpg

from ..theme import TEXT_MUTED, TEXT_DEFAULT, TEXT_WARNING, GAP_PENDING, GAP_CORRECTED, GAP_MISSING
from .. import state as state_mod


_BAR_TAG = "status_bar_window"


class StatusBar:
    """Thin bottom bar showing live stats."""

    def __init__(self, app_state: state_mod.AppState,
                 width: int = 1600, height: int = 30):
        self.state  = app_state
        self.width  = width
        self.height = height

        self.state.on(state_mod.EVT_PAGE_CHANGED,  self._refresh)
        self.state.on(state_mod.EVT_GAP_EDITED,    self._refresh)
        self.state.on(state_mod.EVT_DIRTY_CHANGED, self._refresh)

    def build(self, parent):
        with dpg.child_window(tag=_BAR_TAG, parent=parent,
                              width=self.width, height=self.height,
                              no_scrollbar=True):
            with dpg.group(horizontal=True):
                dpg.add_text("Gaps:", color=TEXT_MUTED)
                dpg.add_text("—", tag="sb_pending",   color=GAP_PENDING)
                dpg.add_text("pending  ", color=TEXT_MUTED)
                dpg.add_text("—", tag="sb_corrected", color=GAP_CORRECTED)
                dpg.add_text("corrected  ", color=TEXT_MUTED)
                dpg.add_text("—", tag="sb_missing",   color=GAP_MISSING)
                dpg.add_text("missing  ", color=TEXT_MUTED)
                dpg.add_spacer(width=20)
                dpg.add_text("Total:", color=TEXT_MUTED)
                dpg.add_text("—", tag="sb_total", color=TEXT_DEFAULT)
                dpg.add_spacer(width=20)
                dpg.add_text("", tag="sb_complete_flag", color=GAP_CORRECTED)

    def _refresh(self, **_):
        doc = self.state.page_doc
        if not doc:
            return
        stats = doc.gap_stats()
        _set("sb_pending",   str(stats.get("pending", 0)))
        _set("sb_corrected", str(stats.get("corrected", 0)))
        _set("sb_missing",   str(stats.get("missing", 0)))
        _set("sb_total",     str(stats.get("total", 0)))
        if self.state.is_current_page_complete():
            _set("sb_complete_flag", "  ✓ PAGE COMPLETE")
        else:
            _set("sb_complete_flag", "")


def _set(tag: str, value: str):
    if dpg.does_item_exist(tag):
        dpg.set_value(tag, value)
