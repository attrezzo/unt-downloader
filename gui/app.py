"""
gui/app.py — Main application window and render loop.

Layout (top to bottom):
  ┌─────────────────────────────────────────────────────┐
  │  Toolbar (navigation, save, modes, completion)      │
  ├──────────────────────┬──────────────────────────────┤
  │  Image Panel         │  Text Panel (upper)          │
  │  (left ~57%)         │  + Gap Inspector (lower)     │
  │                      │  + Map Editor (mode=map)     │
  ├──────────────────────┴──────────────────────────────┤
  │  Status bar                                         │
  └─────────────────────────────────────────────────────┘
"""

from __future__ import annotations
from pathlib import Path
from typing import Optional

import dearpygui.dearpygui as dpg

from .state import AppState, MODE_VIEW, MODE_MAP, MODE_MEASURE
from .panels.image_panel import ImagePanel
from .panels.text_panel import TextPanel
from .panels.gap_inspector import GapInspector
from .panels.map_editor import MapEditor
from .panels.toolbar import Toolbar
from .panels.status_bar import StatusBar
from .theme import PANEL_BG, TOOLBAR_BG


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_VIEWPORT_W = 1680
_VIEWPORT_H = 980
_TOOLBAR_H  = 54
_STATUSBAR_H = 28
_CONTENT_H  = _VIEWPORT_H - _TOOLBAR_H - _STATUSBAR_H - 16

_IMG_PANEL_W  = int(_VIEWPORT_W * 0.55)
_TEXT_PANEL_W = _VIEWPORT_W - _IMG_PANEL_W - 20

_INSPECTOR_H = 360
_TEXT_H      = _CONTENT_H - _INSPECTOR_H - 10


class App:
    """Application shell. One instance, owns the DPG context."""

    def __init__(self):
        self.state     = AppState()
        self._toolbar  = Toolbar(self.state, width=_VIEWPORT_W, height=_TOOLBAR_H)
        self._img      = ImagePanel(self.state, width=_IMG_PANEL_W, height=_CONTENT_H)
        self._text     = TextPanel(self.state, width=_TEXT_PANEL_W,
                                   height=_TEXT_H + _INSPECTOR_H)
        self._inspector = GapInspector(self.state, width=_TEXT_PANEL_W,
                                       height=_INSPECTOR_H)
        self._map_ed   = MapEditor(self.state, self._img,
                                   width=_TEXT_PANEL_W, height=_CONTENT_H)
        self._statusbar = StatusBar(self.state, width=_VIEWPORT_W, height=_STATUSBAR_H)

        # Link map_editor back into image_panel
        self._img.image_panel = self._map_ed   # not used directly but kept for symmetry

    # ------------------------------------------------------------------
    # Initialise
    # ------------------------------------------------------------------

    def init(self):
        dpg.create_context()

        # Global keyboard handlers
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_S,
                callback=lambda s, a: self._on_key_s(a))
            dpg.add_key_press_handler(dpg.mvKey_Z,
                callback=lambda s, a: self._on_key_z(a))

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def build(self):
        with dpg.window(tag="primary_window",
                        no_title_bar=True, no_resize=True,
                        no_move=True, no_close=True,
                        width=_VIEWPORT_W, height=_VIEWPORT_H):

            # --- Toolbar ---
            self._toolbar.build("primary_window")

            # --- Main content row ---
            with dpg.group(horizontal=True):

                # Left: image panel
                self._img.build("primary_window")    # builds inside group

                # Right: stacked panels
                with dpg.group():
                    # Text panel (upper)
                    self._text.build("primary_window")
                    # Gap Inspector (lower right)
                    self._inspector.build("primary_window")
                    # Map editor (replaces inspector in map mode — hidden by default)
                    self._map_ed.build("primary_window")

            # --- Status bar ---
            self._statusbar.build("primary_window")

        # --- Modals ---
        self._build_open_collection_modal()
        self._build_completion_modal()

        # --- Menu bar ---
        with dpg.viewport_menu_bar():
            with dpg.menu(label="File"):
                dpg.add_menu_item(label="Open Collection…",
                                  callback=lambda: dpg.configure_item(
                                      "open_collection_modal", show=True))
                dpg.add_menu_item(label="Save",
                                  callback=lambda: self.state.save())
                dpg.add_separator()
                dpg.add_menu_item(label="Quit",
                                  callback=lambda: dpg.stop_dearpygui())
            with dpg.menu(label="View"):
                dpg.add_menu_item(label="Fit Image",
                                  callback=lambda: self._img.fit_image())
                dpg.add_menu_item(label="Zoom In",
                                  callback=lambda: self._img.zoom_in())
                dpg.add_menu_item(label="Zoom Out",
                                  callback=lambda: self._img.zoom_out())
            with dpg.menu(label="Mode"):
                dpg.add_menu_item(label="View Mode",
                                  callback=lambda: self.state.set_mode(MODE_VIEW))
                dpg.add_menu_item(label="Map Edit Mode",
                                  callback=lambda: self.state.set_mode(MODE_MAP))
                dpg.add_menu_item(label="Measure Mode",
                                  callback=lambda: self.state.set_mode(MODE_MEASURE))
            with dpg.menu(label="Help"):
                dpg.add_menu_item(label="About",
                                  callback=self._show_about)

    def _build_open_collection_modal(self):
        with dpg.window(tag="open_collection_modal",
                        label="Open Collection",
                        modal=True, show=False,
                        width=500, height=140,
                        pos=[_VIEWPORT_W // 2 - 250, _VIEWPORT_H // 2 - 70]):
            dpg.add_text("Collection directory path:")
            dpg.add_input_text(tag="open_coll_path",
                               hint="/path/to/bellville_wochenblatt",
                               width=460)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Open",
                               callback=self._on_open_collection)
                dpg.add_button(label="Cancel",
                               callback=lambda: dpg.configure_item(
                                   "open_collection_modal", show=False))

    def _build_completion_modal(self):
        with dpg.window(tag="completion_modal",
                        label="Mark Page Complete",
                        modal=True, show=False,
                        width=500, height=160,
                        pos=[_VIEWPORT_W // 2 - 250, _VIEWPORT_H // 2 - 80]):
            dpg.add_text("Notes (optional):")
            dpg.add_input_text(tag="completion_notes",
                               hint="e.g. Masthead illegible, rest verified",
                               width=460)
            dpg.add_text("This page will be skipped by future automated processing.",
                         color=(200, 200, 100, 255))
            with dpg.group(horizontal=True):
                dpg.add_button(label="Confirm — Mark Complete",
                               callback=self._on_confirm_complete)
                dpg.add_button(label="Cancel",
                               callback=lambda: dpg.configure_item(
                                   "completion_modal", show=False))

    # ------------------------------------------------------------------
    # Modal callbacks
    # ------------------------------------------------------------------

    def _on_open_collection(self, *_):
        path_str = dpg.get_value("open_coll_path").strip()
        if path_str:
            p = Path(path_str)
            if p.is_dir():
                self.state.load_collection(p)
                dpg.configure_item("open_collection_modal", show=False)
            else:
                dpg.set_value("open_coll_path",
                              f"[Not found] {path_str}")

    def _on_confirm_complete(self, *_):
        notes = dpg.get_value("completion_notes")
        self.state.mark_page_complete(notes=notes)
        dpg.configure_item("completion_modal", show=False)

    # ------------------------------------------------------------------
    # Keyboard shortcuts
    # ------------------------------------------------------------------

    def _on_key_s(self, app_data):
        if dpg.is_key_down(dpg.mvKey_Control):
            self.state.save()

    def _on_key_z(self, app_data):
        if dpg.is_key_down(dpg.mvKey_Control):
            if dpg.is_key_down(dpg.mvKey_Shift):
                self.state.redo()
            else:
                self.state.undo()

    # ------------------------------------------------------------------
    # About
    # ------------------------------------------------------------------

    def _show_about(self, *_):
        with dpg.window(label="About", modal=True, width=400, height=120,
                        pos=[_VIEWPORT_W // 2 - 200, _VIEWPORT_H // 2 - 60],
                        on_close=lambda: dpg.delete_item("about_win")):
            dpg.add_text("UNT Archive OCR Correction GUI")
            dpg.add_text("Human-in-the-loop review tool for the UNT re-OCR pipeline.")
            dpg.add_text("Collection: texashistory.unt.edu")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self, collection_path: Optional[Path] = None):
        self.init()
        self.build()

        dpg.create_viewport(
            title="UNT OCR Correction",
            width=_VIEWPORT_W,
            height=_VIEWPORT_H,
            min_width=800,
            min_height=600,
        )
        dpg.setup_dearpygui()
        dpg.set_primary_window("primary_window", True)
        dpg.show_viewport()

        if collection_path:
            # Load after viewport is shown so textures can be registered
            dpg.split_frame()
            try:
                self.state.load_collection(collection_path)
            except Exception as e:
                print(f"[app] Failed to load collection: {e}")

        dpg.start_dearpygui()
        dpg.destroy_context()
