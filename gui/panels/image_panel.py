"""
gui/panels/image_panel.py — Left panel: zoomable/pannable document image viewer.

Renders the page scan as a DPG texture on a drawlist.
Overlays coloured rectangles for:
  - Gap regions (colour-coded by status)
  - Selected word bbox (blue)
  - Document map regions (semi-transparent fills)
  - Active gap selection (bright yellow outline)

Zoom/pan: mouse wheel zooms centred on cursor; left-drag pans.
Modes: VIEW | MAP_EDIT | MEASURE
"""

from __future__ import annotations
import io
from pathlib import Path
from typing import Optional, Callable

import dearpygui.dearpygui as dpg

from ..theme import (GAP_STATUS_COLORS, SELECTION_HIGHLIGHT, ACTIVE_GAP_OUTLINE,
                     REGION_COLORS, REGION_BORDER_COLORS, conf_color)
from ..models.gap import GapData
from ..models.map_file import DocumentMap, Geometry, REGION_TYPES
from ..models.char_measure import CharMeasurement
from .. import state as state_mod


class ImagePanel:
    """Left-side image viewer. Owns its DPG child window and drawlist."""

    _TEXTURE_TAG  = "img_panel_texture"
    _DRAWLIST_TAG = "img_panel_drawlist"
    _WINDOW_TAG   = "img_panel_window"

    def __init__(self, app_state: state_mod.AppState,
                 width: int = 900, height: int = 900):
        self.state  = app_state
        self.width  = width
        self.height = height

        # View transform
        self._zoom   = 1.0
        self._pan_x  = 0.0
        self._pan_y  = 0.0
        self._img_w  = 1
        self._img_h  = 1
        self._texture_registered = False

        # Map edit draw state
        self._drawing      = False
        self._draw_start   = None    # (img_x, img_y) image-space
        self._pending_poly: list = []
        self._new_region_type = "column"

        # Char measurement
        self.measurement = CharMeasurement()
        self._measure_callback: Optional[Callable] = None

        # Drag pan
        self._drag_start_pan  = None
        self._drag_start_mouse = None

        # Register state callbacks
        self.state.on(state_mod.EVT_PAGE_CHANGED,    self._on_page_changed)
        self.state.on(state_mod.EVT_GAP_EDITED,      self._on_gap_changed)
        self.state.on(state_mod.EVT_GAP_SELECTED,    self._on_gap_changed)
        self.state.on(state_mod.EVT_WORD_SELECTED,   self._on_word_selected)
        self.state.on(state_mod.EVT_MAP_CHANGED,     self._on_gap_changed)
        self.state.on(state_mod.EVT_MODE_CHANGED,    self._on_mode_changed)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self, parent):
        with dpg.child_window(tag=self._WINDOW_TAG, parent=parent,
                              width=self.width, height=self.height,
                              no_scrollbar=True):
            dpg.add_drawlist(tag=self._DRAWLIST_TAG,
                             width=self.width, height=self.height)

        # Mouse handlers on the drawlist
        with dpg.handler_registry():
            dpg.add_mouse_wheel_handler(callback=self._on_wheel)
            dpg.add_mouse_click_handler(callback=self._on_click)
            dpg.add_mouse_drag_handler(callback=self._on_drag)
            dpg.add_mouse_release_handler(callback=self._on_release)

    # ------------------------------------------------------------------
    # Image loading
    # ------------------------------------------------------------------

    def _load_image(self, path: Path):
        """Load JPEG from path into a DPG static texture."""
        try:
            from PIL import Image
            img = Image.open(path).convert("RGBA")
            # Downscale very large images for display (keep orignal for coords)
            MAX_DIM = 3000
            orig_w, orig_h = img.size
            scale = min(MAX_DIM / orig_w, MAX_DIM / orig_h, 1.0)
            if scale < 1.0:
                nw = int(orig_w * scale)
                nh = int(orig_h * scale)
                img = img.resize((nw, nh), Image.LANCZOS)

            self._img_w, self._img_h = img.size
            self._display_scale = scale  # for coord transform

            data = list(img.getdata())
            flat = []
            for r, g, b, a in data:
                flat += [r / 255, g / 255, b / 255, a / 255]

            if self._texture_registered:
                dpg.delete_item(self._TEXTURE_TAG)
            with dpg.texture_registry():
                dpg.add_static_texture(
                    width=self._img_w, height=self._img_h,
                    default_value=flat, tag=self._TEXTURE_TAG)
            self._texture_registered = True
            self._fit_to_panel()
        except Exception as e:
            print(f"[image_panel] Failed to load image: {e}")

    def _fit_to_panel(self):
        """Zoom/pan so the image fits the panel."""
        w_scale = (self.width - 10)  / (self._img_w or 1)
        h_scale = (self.height - 10) / (self._img_h or 1)
        self._zoom  = min(w_scale, h_scale)
        self._pan_x = (self.width  - self._img_w * self._zoom) / 2
        self._pan_y = (self.height - self._img_h * self._zoom) / 2
        self.redraw()

    # ------------------------------------------------------------------
    # Coordinate transforms
    # ------------------------------------------------------------------

    def img_to_screen(self, ix: float, iy: float) -> tuple:
        """Image-space → screen-space (drawlist coordinates)."""
        return (ix * self._zoom + self._pan_x,
                iy * self._zoom + self._pan_y)

    def screen_to_img(self, sx: float, sy: float) -> tuple:
        """Screen-space → image-space."""
        return ((sx - self._pan_x) / (self._zoom or 1),
                (sy - self._pan_y) / (self._zoom or 1))

    def page_to_img(self, px: int, py: int) -> tuple:
        """Page-absolute pixel → display image coords (accounts for downscale)."""
        scale = getattr(self, "_display_scale", 1.0)
        return (px * scale, py * scale)

    def page_bbox_to_screen(self, x: int, y: int, w: int, h: int) -> tuple:
        """Page-absolute (x,y,w,h) → (sx0,sy0,sx1,sy1) screen coords."""
        ix0, iy0 = self.page_to_img(x, y)
        ix1, iy1 = self.page_to_img(x + w, y + h)
        sx0, sy0 = self.img_to_screen(ix0, iy0)
        sx1, sy1 = self.img_to_screen(ix1, iy1)
        return (sx0, sy0, sx1, sy1)

    # ------------------------------------------------------------------
    # Drawing
    # ------------------------------------------------------------------

    def redraw(self):
        if not dpg.does_item_exist(self._DRAWLIST_TAG):
            return
        dpg.delete_item(self._DRAWLIST_TAG, children_only=True)

        # Background
        dpg.draw_rectangle([0, 0], [self.width, self.height],
                            fill=(20, 20, 22, 255),
                            parent=self._DRAWLIST_TAG)

        # Image
        if self._texture_registered:
            sx0, sy0 = self.img_to_screen(0, 0)
            sx1, sy1 = self.img_to_screen(self._img_w, self._img_h)
            dpg.draw_image(self._TEXTURE_TAG, [sx0, sy0], [sx1, sy1],
                           parent=self._DRAWLIST_TAG)

        # Overlays
        self._draw_map_regions()
        self._draw_gap_overlays()
        self._draw_word_highlight()
        self._draw_measure_overlay()
        self._draw_in_progress_region()

    def _draw_gap_overlays(self):
        if not self.state.page_doc:
            return
        for gap in self.state.page_doc.gaps:
            if not gap.imgbbox or gap.imgbbox == "0,0,0,0":
                continue
            from gap_utils import parse_bbox
            x, y, w, h = parse_bbox(gap.imgbbox)
            if w <= 0 or h <= 0:
                continue
            sx0, sy0, sx1, sy1 = self.page_bbox_to_screen(x, y, w, h)
            color = GAP_STATUS_COLORS.get(gap.status, GAP_STATUS_COLORS[""])
            fill  = (*color[:3], 60)
            dpg.draw_rectangle([sx0, sy0], [sx1, sy1],
                                color=color, fill=fill, thickness=2,
                                parent=self._DRAWLIST_TAG)
            # Selected gap gets a bright outline
            if gap.gap_id == self.state.selected_gap_id:
                dpg.draw_rectangle([sx0 - 2, sy0 - 2], [sx1 + 2, sy1 + 2],
                                    color=ACTIVE_GAP_OUTLINE, thickness=3,
                                    parent=self._DRAWLIST_TAG)
            # Status label
            label = gap.display_status[:3].upper()
            dpg.draw_text([sx0 + 2, sy0 + 2], label, size=10,
                           color=(255, 255, 255, 200),
                           parent=self._DRAWLIST_TAG)

    def _draw_word_highlight(self):
        if self.state.selected_word_idx is None:
            return
        if not self.state.page_doc:
            return
        bboxes = self.state.page_doc.word_bboxes
        idx = self.state.selected_word_idx
        if idx >= len(bboxes):
            return
        wb = bboxes[idx]
        sx0, sy0, sx1, sy1 = self.page_bbox_to_screen(wb.x, wb.y, wb.w, wb.h)
        dpg.draw_rectangle([sx0, sy0], [sx1, sy1],
                            color=SELECTION_HIGHLIGHT,
                            fill=(*SELECTION_HIGHLIGHT[:3], 80),
                            thickness=2,
                            parent=self._DRAWLIST_TAG)

    def _draw_map_regions(self):
        doc_map = self.state.doc_map
        if not doc_map:
            return
        for region in doc_map.regions:
            geom = region.geometry
            fill   = REGION_COLORS.get(region.type, REGION_COLORS["other"])
            border = REGION_BORDER_COLORS.get(region.type, (200, 200, 200, 200))
            if geom.kind == "rect":
                sx0, sy0, sx1, sy1 = self.page_bbox_to_screen(
                    geom.x, geom.y, geom.w, geom.h)
                dpg.draw_rectangle([sx0, sy0], [sx1, sy1],
                                    color=border, fill=fill, thickness=1,
                                    parent=self._DRAWLIST_TAG)
                dpg.draw_text([sx0 + 4, sy0 + 4],
                               f"{region.type}: {region.label}",
                               size=11, color=(255, 255, 255, 200),
                               parent=self._DRAWLIST_TAG)
            elif geom.kind == "polygon" and len(geom.points) >= 3:
                screen_pts = [list(self.img_to_screen(*self.page_to_img(p[0], p[1])))
                              for p in geom.points]
                dpg.draw_polygon(screen_pts, color=border, fill=fill,
                                  thickness=1, parent=self._DRAWLIST_TAG)

    def _draw_measure_overlay(self):
        if self.state.mode != state_mod.MODE_MEASURE:
            return
        m = self.measurement
        if m.point_a:
            ix, iy = self.page_to_img(*m.point_a)
            sx, sy = self.img_to_screen(ix, iy)
            dpg.draw_circle([sx, sy], 5, color=(255, 220, 0, 255),
                             fill=(255, 220, 0, 200),
                             parent=self._DRAWLIST_TAG)
        if m.point_a and m.point_b:
            ia = self.page_to_img(*m.point_a)
            ib = self.page_to_img(*m.point_b)
            sa = self.img_to_screen(*ia)
            sb = self.img_to_screen(*ib)
            dpg.draw_line(list(sa), list(sb), color=(255, 220, 0, 255),
                           thickness=2, parent=self._DRAWLIST_TAG)
            dpg.draw_circle(list(sb), 5, color=(255, 220, 0, 255),
                             fill=(255, 220, 0, 200),
                             parent=self._DRAWLIST_TAG)
            # Label
            label = str(m)
            mid_x = (sa[0] + sb[0]) / 2
            mid_y = (sa[1] + sb[1]) / 2 - 14
            dpg.draw_text([mid_x, mid_y], label, size=11,
                           color=(255, 220, 0, 255),
                           parent=self._DRAWLIST_TAG)

    def _draw_in_progress_region(self):
        """Draw rect/polygon being drawn in map edit mode."""
        if self.state.mode != state_mod.MODE_MAP:
            return
        if self._draw_start and dpg.is_mouse_button_down(0):
            mx, my = dpg.get_mouse_pos(local=False)
            # Get drawlist local position
            dl_pos = dpg.get_item_rect_min(self._DRAWLIST_TAG)
            lx = mx - dl_pos[0]
            ly = my - dl_pos[1]
            sx0, sy0 = self.img_to_screen(*self.page_to_img(*self._draw_start))
            border = REGION_BORDER_COLORS.get(self._new_region_type, (200,200,200,200))
            fill   = REGION_COLORS.get(self._new_region_type, (200,200,200,60))
            dpg.draw_rectangle([sx0, sy0], [lx, ly],
                                color=border, fill=fill, thickness=2,
                                parent=self._DRAWLIST_TAG)

    # ------------------------------------------------------------------
    # Mouse event handlers
    # ------------------------------------------------------------------

    def _on_wheel(self, sender, app_data):
        if not dpg.is_item_hovered(self._DRAWLIST_TAG):
            return
        delta = app_data
        factor = 1.1 if delta > 0 else 0.9
        # Zoom centred on mouse position
        mx, my = dpg.get_mouse_pos(local=False)
        dl_pos = dpg.get_item_rect_min(self._DRAWLIST_TAG)
        lx = mx - dl_pos[0]
        ly = my - dl_pos[1]
        self._pan_x = lx - (lx - self._pan_x) * factor
        self._pan_y = ly - (ly - self._pan_y) * factor
        self._zoom *= factor
        self._zoom  = max(0.05, min(self._zoom, 20.0))
        self.redraw()

    def _on_click(self, sender, app_data):
        button = app_data
        if not dpg.is_item_hovered(self._DRAWLIST_TAG):
            return
        mx, my = dpg.get_mouse_pos(local=False)
        dl_pos = dpg.get_item_rect_min(self._DRAWLIST_TAG)
        lx, ly = mx - dl_pos[0], my - dl_pos[1]
        ix, iy = self.screen_to_img(lx, ly)
        # Page-absolute pixel coords (undo display downscale)
        scale = getattr(self, "_display_scale", 1.0)
        px, py = int(ix / scale), int(iy / scale)

        mode = self.state.mode

        if mode == state_mod.MODE_MEASURE and button == 0:
            self.measurement.click(px, py)
            self.redraw()
            return

        if mode == state_mod.MODE_MAP and button == 0:
            self._handle_map_click(px, py)
            return

        if mode == state_mod.MODE_VIEW:
            if button == 0:
                self._handle_view_click(px, py)
            elif button == 1:
                self._handle_right_click(px, py)

    def _handle_view_click(self, px: int, py: int):
        """Left click in view mode: select gap or word under cursor."""
        from ..linking import LinkingEngine
        if self.state.page_doc:
            engine = LinkingEngine(self.state.page_doc)
            entry = engine.image_click_to_entry(px, py)
            if entry:
                if entry.kind == "gap":
                    self.state.select_gap(entry.ref_id)
                elif entry.kind == "word":
                    self.state.select_word(int(entry.ref_id))
            else:
                self.state.select_gap(None)

    def _handle_right_click(self, px: int, py: int):
        """Right click: show context menu."""
        # Will be wired to DPG popup in app.py
        pass

    def _handle_map_click(self, px: int, py: int):
        """Left click in map edit mode: start/finish a rectangle."""
        if self._draw_start is None:
            self._draw_start = (px, py)
        else:
            # Finish the rectangle
            x0, y0 = self._draw_start
            x1, y1 = px, py
            rx, ry = min(x0, x1), min(y0, y1)
            rw, rh = abs(x1 - x0), abs(y1 - y0)
            if rw > 5 and rh > 5 and self.state.doc_map:
                self.state.doc_map.add_region(
                    type_=self._new_region_type,
                    label=self._new_region_type,
                    geometry=Geometry(kind="rect", x=rx, y=ry, w=rw, h=rh),
                )
                self.state.notify(state_mod.EVT_MAP_CHANGED)
            self._draw_start = None
            self.redraw()

    def _on_drag(self, sender, app_data):
        if not dpg.is_item_hovered(self._DRAWLIST_TAG):
            return
        if self.state.mode != state_mod.MODE_VIEW:
            return
        if dpg.is_mouse_button_down(1):   # right-drag to pan
            dx, dy = app_data[1], app_data[2]
            self._pan_x += dx
            self._pan_y += dy
            self.redraw()

    def _on_release(self, sender, app_data):
        self.redraw()

    # ------------------------------------------------------------------
    # State callbacks
    # ------------------------------------------------------------------

    def _on_page_changed(self, doc, page, **_):
        if doc and doc.image_path:
            self._load_image(doc.image_path)
        self.redraw()

    def _on_gap_changed(self, **_):
        self.redraw()

    def _on_word_selected(self, **_):
        self.redraw()

    def _on_mode_changed(self, mode, **_):
        self._draw_start = None
        self.measurement.reset()
        self.redraw()

    # ------------------------------------------------------------------
    # Public helpers for toolbar
    # ------------------------------------------------------------------

    def set_region_type(self, region_type: str):
        self._new_region_type = region_type

    def fit_image(self):
        self._fit_to_panel()

    def zoom_in(self):
        cx, cy = self.width / 2, self.height / 2
        self._pan_x = cx - (cx - self._pan_x) * 1.25
        self._pan_y = cy - (cy - self._pan_y) * 1.25
        self._zoom *= 1.25
        self.redraw()

    def zoom_out(self):
        cx, cy = self.width / 2, self.height / 2
        self._pan_x = cx - (cx - self._pan_x) * 0.8
        self._pan_y = cy - (cy - self._pan_y) * 0.8
        self._zoom *= 0.8
        self.redraw()

    def highlight_page_bbox(self, bbox: tuple):
        """Externally request a highlight (e.g. from text panel word click)."""
        self.state.hovered_bbox = bbox
        self.redraw()
