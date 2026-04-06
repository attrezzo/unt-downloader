"""
gui/panels/toolbar.py — Top toolbar: navigation, save, mode toggles, completion.
"""

from __future__ import annotations
from typing import Optional

import dearpygui.dearpygui as dpg

from ..theme import (BUTTON_PRIMARY, BUTTON_DANGER, BUTTON_SUCCESS,
                     TEXT_DEFAULT, TEXT_MUTED, TEXT_WARNING)
from .. import state as state_mod


_TOOLBAR_TAG   = "toolbar_window"
_ISSUE_COMBO   = "toolbar_issue_combo"
_PAGE_TEXT     = "toolbar_page_text"
_DIRTY_LABEL   = "toolbar_dirty_label"
_COMPLETE_LABEL = "toolbar_complete_label"
_MEASURE_STATUS = "toolbar_measure_status"


class Toolbar:
    """Top toolbar with navigation controls."""

    def __init__(self, app_state: state_mod.AppState,
                 width: int = 1600, height: int = 60):
        self.state  = app_state
        self.width  = width
        self.height = height

        self.state.on(state_mod.EVT_COLLECTION_LOADED, self._on_collection)
        self.state.on(state_mod.EVT_ISSUE_CHANGED,     self._on_issue)
        self.state.on(state_mod.EVT_PAGE_CHANGED,      self._on_page)
        self.state.on(state_mod.EVT_DIRTY_CHANGED,     self._on_dirty)
        self.state.on(state_mod.EVT_COMPLETION_CHANGED, self._on_completion)
        self.state.on(state_mod.EVT_MODE_CHANGED,      self._on_mode)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, parent):
        with dpg.child_window(tag=_TOOLBAR_TAG, parent=parent,
                              width=self.width, height=self.height,
                              no_scrollbar=True):
            with dpg.group(horizontal=True):

                # Collection label
                dpg.add_text("Collection:", color=TEXT_MUTED)
                dpg.add_text("—", tag="toolbar_coll_name", color=TEXT_DEFAULT)
                dpg.add_spacer(width=10)

                # Issue navigation
                dpg.add_button(label="◀ Issue", callback=self._prev_issue)
                dpg.add_combo(
                    tag=_ISSUE_COMBO,
                    items=[],
                    default_value="",
                    width=280,
                    callback=self._on_issue_selected,
                )
                dpg.add_button(label="Issue ▶", callback=self._next_issue)
                dpg.add_spacer(width=10)

                # Page navigation
                dpg.add_button(label="◀", callback=self._prev_page)
                dpg.add_text("Page:", color=TEXT_MUTED)
                dpg.add_text("—", tag=_PAGE_TEXT, color=TEXT_DEFAULT)
                dpg.add_button(label="▶", callback=self._next_page)
                dpg.add_spacer(width=10)

                # Save
                dpg.add_button(label="💾 Save", callback=self._save)
                dpg.add_text("", tag=_DIRTY_LABEL, color=TEXT_WARNING)
                dpg.add_spacer(width=10)

                # Mode toggles
                dpg.add_button(label="View",    callback=self._mode_view)
                dpg.add_button(label="Map Edit", callback=self._mode_map)
                dpg.add_button(label="Measure",  callback=self._mode_measure)
                dpg.add_spacer(width=10)

                # Measure status
                dpg.add_text("", tag=_MEASURE_STATUS, color=TEXT_WARNING)
                dpg.add_spacer(width=10)

                # Completion
                dpg.add_button(label="Mark Page Complete",
                               callback=self._mark_page_complete)
                dpg.add_button(label="Mark Issue Complete",
                               callback=self._mark_issue_complete)
                dpg.add_text("", tag=_COMPLETE_LABEL, color=BUTTON_SUCCESS)

                # Undo / Redo
                dpg.add_spacer(width=10)
                dpg.add_button(label="↩ Undo", callback=self._undo)
                dpg.add_button(label="↪ Redo", callback=self._redo)

    # ------------------------------------------------------------------
    # Navigation callbacks
    # ------------------------------------------------------------------

    def _prev_issue(self, *_):  self.state.prev_issue()
    def _next_issue(self, *_):  self.state.next_issue()
    def _prev_page(self, *_):   self.state.prev_page()
    def _next_page(self, *_):   self.state.next_page()

    def _on_issue_selected(self, sender, app_data, user_data):
        # Find index matching the selected label
        for i, issue in enumerate(self.state.issues):
            label = _issue_label(issue)
            if label == app_data:
                self.state.go_to_issue(i)
                return

    def _save(self, *_):
        self.state.save()

    def _undo(self, *_): self.state.undo()
    def _redo(self, *_): self.state.redo()

    # ------------------------------------------------------------------
    # Mode toggles
    # ------------------------------------------------------------------

    def _mode_view(self, *_):    self.state.set_mode(state_mod.MODE_VIEW)
    def _mode_map(self, *_):     self.state.set_mode(state_mod.MODE_MAP)
    def _mode_measure(self, *_): self.state.set_mode(state_mod.MODE_MEASURE)

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def _mark_page_complete(self, *_):
        dpg.configure_item("completion_modal", show=True)

    def _mark_issue_complete(self, *_):
        self.state.mark_issue_complete()

    # ------------------------------------------------------------------
    # State change callbacks
    # ------------------------------------------------------------------

    def _on_collection(self, issues, cfg, **_):
        if dpg.does_item_exist("toolbar_coll_name"):
            dpg.set_value("toolbar_coll_name",
                          cfg.get("title_name", "Unknown"))
        if dpg.does_item_exist(_ISSUE_COMBO):
            labels = [_issue_label(i) for i in issues]
            dpg.configure_item(_ISSUE_COMBO, items=labels)
            if labels:
                dpg.set_value(_ISSUE_COMBO, labels[0])

    def _on_issue(self, issue, index, **_):
        if dpg.does_item_exist(_ISSUE_COMBO) and issue:
            dpg.set_value(_ISSUE_COMBO, _issue_label(issue))

    def _on_page(self, doc, page, **_):
        if dpg.does_item_exist(_PAGE_TEXT):
            total = self.state.total_pages
            dpg.set_value(_PAGE_TEXT, f"{page} / {total}")
        self._on_completion()

    def _on_dirty(self, dirty, **_):
        if dpg.does_item_exist(_DIRTY_LABEL):
            dpg.set_value(_DIRTY_LABEL, "● unsaved" if dirty else "")

    def _on_completion(self, **_):
        if not dpg.does_item_exist(_COMPLETE_LABEL):
            return
        if self.state.is_current_page_complete():
            dpg.set_value(_COMPLETE_LABEL, "✓ complete")
        else:
            dpg.set_value(_COMPLETE_LABEL, "")

    def _on_mode(self, mode, **_):
        # Update measure status hint
        if dpg.does_item_exist(_MEASURE_STATUS):
            if mode == state_mod.MODE_MEASURE:
                dpg.set_value(_MEASURE_STATUS, "Click 2 points on image to measure")
            else:
                dpg.set_value(_MEASURE_STATUS, "")


def _issue_label(issue: dict) -> str:
    date = issue.get("date", "?")
    vol  = issue.get("volume", "?")
    num  = issue.get("number", "?")
    return f"{date}  Vol {vol} No {num}"
