"""
gui/panels/map_editor.py — Document map region management sidebar.

A docked panel that lists all regions defined for the current page.
Works in concert with image_panel.py which handles the drawing interaction.

Provides:
  - Region list with type/label/char-size display
  - Delete button per region
  - Region type selector (feeds back to image_panel drawing mode)
  - Character size input per region
"""

from __future__ import annotations
from typing import Optional

import dearpygui.dearpygui as dpg

from ..theme import REGION_COLORS, TEXT_DEFAULT, TEXT_MUTED, BUTTON_DANGER
from ..models.map_file import REGION_TYPES, Region
from .. import state as state_mod


_PANEL_TAG = "map_editor_panel"


class MapEditor:
    """Region list and map controls. Shown when mode == MODE_MAP."""

    def __init__(self, app_state: state_mod.AppState,
                 image_panel=None,
                 width: int = 300, height: int = 400):
        self.state       = app_state
        self.image_panel = image_panel   # set after construction
        self.width       = width
        self.height      = height

        self.state.on(state_mod.EVT_MAP_CHANGED,   self._refresh)
        self.state.on(state_mod.EVT_PAGE_CHANGED,  self._refresh)
        self.state.on(state_mod.EVT_MODE_CHANGED,  self._on_mode_changed)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, parent):
        with dpg.child_window(tag=_PANEL_TAG, parent=parent,
                              width=self.width, height=self.height,
                              show=False):
            dpg.add_text("Document Map Editor", color=TEXT_MUTED)
            dpg.add_separator()

            # New region type selector
            dpg.add_text("Draw region type:", color=TEXT_MUTED)
            dpg.add_combo(
                tag="map_type_selector",
                items=REGION_TYPES,
                default_value="column",
                width=self.width - 20,
                callback=self._on_type_changed,
            )
            dpg.add_text("Click image twice (top-left → bottom-right) to draw",
                         color=TEXT_MUTED, wrap=self.width - 20)
            dpg.add_separator()

            # Region list
            dpg.add_text("Defined regions:", color=TEXT_MUTED)
            with dpg.child_window(tag="map_region_list",
                                   width=self.width - 10,
                                   height=self.height - 140,
                                   border=False):
                pass  # populated by _refresh

    # ------------------------------------------------------------------
    # Refresh region list
    # ------------------------------------------------------------------

    def _refresh(self, **_):
        if not dpg.does_item_exist("map_region_list"):
            return
        dpg.delete_item("map_region_list", children_only=True)

        doc_map = self.state.doc_map
        if not doc_map or not doc_map.regions:
            dpg.add_text("(No regions yet)", color=TEXT_MUTED,
                         parent="map_region_list")
            return

        for region in doc_map.regions:
            self._add_region_row(region)

    def _add_region_row(self, region: Region):
        color = REGION_COLORS.get(region.type, REGION_COLORS["other"])
        border_color = (*color[:3], 255)
        row_tag = f"map_row_{region.id}"

        with dpg.group(tag=row_tag, parent="map_region_list"):
            with dpg.group(horizontal=True):
                # Colour swatch
                with dpg.drawlist(width=12, height=12):
                    dpg.draw_rectangle([0, 0], [12, 12],
                                        fill=color, color=border_color)
                dpg.add_text(f"[{region.type}] {region.label or region.id}",
                             color=TEXT_DEFAULT)
                dpg.add_button(
                    label="X",
                    small=True,
                    callback=self._on_delete,
                    user_data=region.id,
                )

            # Char size inputs
            with dpg.group(horizontal=True):
                dpg.add_text("  char_w:", color=TEXT_MUTED)
                dpg.add_input_float(
                    tag=f"map_cw_{region.id}",
                    default_value=region.char_width_px or 0.0,
                    width=70,
                    min_value=0.0,
                    step=0.5,
                    callback=self._on_char_size_changed,
                    user_data=(region.id, "width"),
                )
                dpg.add_text("  char_h:", color=TEXT_MUTED)
                dpg.add_input_float(
                    tag=f"map_ch_{region.id}",
                    default_value=region.char_height_px or 0.0,
                    width=70,
                    min_value=0.0,
                    step=0.5,
                    callback=self._on_char_size_changed,
                    user_data=(region.id, "height"),
                )
            dpg.add_separator()

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------

    def _on_type_changed(self, sender, app_data, user_data):
        if self.image_panel:
            self.image_panel.set_region_type(app_data)

    def _on_delete(self, sender, app_data, region_id):
        doc_map = self.state.doc_map
        if doc_map:
            doc_map.remove_region(region_id)
            self.state.notify(state_mod.EVT_MAP_CHANGED)
            if self.state.collection_dir:
                doc_map.save(self.state.collection_dir)

    def _on_char_size_changed(self, sender, app_data, user_data):
        region_id, dim = user_data
        doc_map = self.state.doc_map
        if not doc_map:
            return
        region = doc_map.get_region(region_id)
        if not region:
            return
        if dim == "width":
            region.char_width_px  = app_data if app_data > 0 else None
        else:
            region.char_height_px = app_data if app_data > 0 else None
        if self.state.collection_dir:
            doc_map.save(self.state.collection_dir)

    def _on_mode_changed(self, mode, **_):
        if dpg.does_item_exist(_PANEL_TAG):
            dpg.configure_item(_PANEL_TAG, show=(mode == state_mod.MODE_MAP))
