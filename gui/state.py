"""
gui/state.py — Central application state for the OCR correction GUI.

Holds current collection, issue list, active page, and undo stack.
Panels register change callbacks; state.notify() broadcasts updates.
No DPG dependency — plain Python.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Callable

from .models.page_document import PageDocument
from .models.completion import CompletionManifest
from .models.map_file import DocumentMap
from . import loaders


# ---------------------------------------------------------------------------
# Change event types
# ---------------------------------------------------------------------------
EVT_COLLECTION_LOADED = "collection_loaded"
EVT_ISSUE_CHANGED     = "issue_changed"
EVT_PAGE_CHANGED      = "page_changed"
EVT_GAP_EDITED        = "gap_edited"
EVT_GAP_SELECTED      = "gap_selected"
EVT_WORD_SELECTED     = "word_selected"
EVT_MAP_CHANGED       = "map_changed"
EVT_DIRTY_CHANGED     = "dirty_changed"
EVT_COMPLETION_CHANGED = "completion_changed"
EVT_MODE_CHANGED      = "mode_changed"

# Editing modes
MODE_VIEW    = "view"
MODE_MAP     = "map_edit"
MODE_MEASURE = "measure"


class AppState:
    """Central state container. One instance lives for the application lifetime."""

    def __init__(self):
        # Collection
        self.collection_dir: Optional[Path]  = None
        self.collection_cfg: dict            = {}
        self.issues: list                    = []     # all_issues.json entries

        # Navigation
        self.issue_index: int    = 0
        self.page_num:    int    = 1

        # Loaded data
        self.page_doc:    Optional[PageDocument]     = None
        self.completion:  Optional[CompletionManifest] = None
        self.doc_map:     Optional[DocumentMap]      = None

        # UI selection
        self.selected_gap_id:  Optional[str]  = None   # gap_id or None
        self.selected_word_idx: Optional[int] = None   # index into page_doc.word_bboxes
        self.hovered_bbox:     Optional[tuple] = None  # (x,y,w,h) image-space

        # Editing mode
        self.mode: str = MODE_VIEW

        # Undo stack: list of (gap_id, old_tag, new_tag)
        self._undo_stack: list = []
        self._redo_stack: list = []

        # Callbacks: event_type -> list[Callable]
        self._callbacks: dict = {}

    # ------------------------------------------------------------------
    # Observer pattern
    # ------------------------------------------------------------------

    def on(self, event: str, callback: Callable):
        """Register a callback for an event type."""
        self._callbacks.setdefault(event, []).append(callback)

    def off(self, event: str, callback: Callable):
        cbs = self._callbacks.get(event, [])
        if callback in cbs:
            cbs.remove(callback)

    def notify(self, event: str, **data):
        for cb in self._callbacks.get(event, []):
            try:
                cb(**data)
            except Exception as e:
                print(f"[state] callback error on {event}: {e}")

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def load_collection(self, collection_dir: Path):
        self.collection_dir = Path(collection_dir)
        self.collection_cfg = loaders.load_collection_json(self.collection_dir)
        self.issues         = loaders.load_issues(self.collection_dir)
        self.completion     = loaders.load_completion(self.collection_dir)
        self.issue_index    = 0
        self.page_num       = 1
        self.page_doc       = None
        self.doc_map        = None
        self.notify(EVT_COLLECTION_LOADED, issues=self.issues,
                    cfg=self.collection_cfg)
        if self.issues:
            self.go_to_issue(0)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    @property
    def current_issue(self) -> Optional[dict]:
        if not self.issues:
            return None
        return self.issues[self.issue_index]

    def go_to_issue(self, index: int, page: int = 1):
        if not self.issues:
            return
        self.issue_index = max(0, min(index, len(self.issues) - 1))
        self.page_num    = page
        self._undo_stack.clear()
        self._redo_stack.clear()
        self.notify(EVT_ISSUE_CHANGED, issue=self.current_issue,
                    index=self.issue_index)
        self.load_page(page)

    def next_issue(self):
        if self.issue_index < len(self.issues) - 1:
            self.go_to_issue(self.issue_index + 1)

    def prev_issue(self):
        if self.issue_index > 0:
            self.go_to_issue(self.issue_index - 1)

    def go_to_page(self, page_num: int):
        if not self.current_issue:
            return
        total = self.total_pages
        self.page_num = max(1, min(page_num, total))
        self.load_page(self.page_num)

    def next_page(self):
        self.go_to_page(self.page_num + 1)

    def prev_page(self):
        self.go_to_page(self.page_num - 1)

    @property
    def total_pages(self) -> int:
        if not self.current_issue or not self.collection_dir:
            return 1
        return loaders.page_count_for_issue(self.current_issue, self.collection_dir)

    # ------------------------------------------------------------------
    # Page loading
    # ------------------------------------------------------------------

    def load_page(self, page_num: int):
        if not self.current_issue or not self.collection_dir:
            return
        self.page_num    = page_num
        self.page_doc    = loaders.load_page(self.current_issue, page_num,
                                              self.collection_dir)
        self.doc_map     = loaders.load_document_map(
            self.current_issue["ark_id"], page_num, self.collection_dir)
        self.selected_gap_id   = None
        self.selected_word_idx = None
        self.notify(EVT_PAGE_CHANGED, doc=self.page_doc, page=page_num)

    # ------------------------------------------------------------------
    # Gap editing
    # ------------------------------------------------------------------

    def select_gap(self, gap_id: Optional[str]):
        self.selected_gap_id = gap_id
        self.notify(EVT_GAP_SELECTED, gap_id=gap_id)

    def edit_gap(self, gap_id: str, action: str, **kwargs):
        """Apply a gap edit action and push to undo stack.

        action: "accept" | "correct" | "missing" | "split"
        kwargs: text= (for correct), note= (for missing), split_x= (for split)
        """
        if not self.page_doc:
            return
        gap = self._find_gap(gap_id)
        if gap is None:
            return

        old_tag = gap.to_tag()

        if action == "accept":
            gap.accept()
        elif action == "correct":
            gap.correct(kwargs.get("text", ""))
        elif action == "missing":
            gap.mark_missing(kwargs.get("note", ""))
        elif action == "split":
            split_x = kwargs.get("split_x", gap.bbox[0] + gap.bbox[2] // 2)
            left, right = gap.split(split_x)
            # Insert children into gap list after parent
            idx = self.page_doc.gaps.index(gap)
            self.page_doc.gaps.insert(idx + 1, right)
            self.page_doc.gaps.insert(idx + 1, left)

        new_tag = gap.to_tag()
        self._undo_stack.append((gap_id, old_tag, new_tag))
        self._redo_stack.clear()
        self.page_doc._dirty = True
        self.notify(EVT_GAP_EDITED, gap_id=gap_id, action=action)
        self.notify(EVT_DIRTY_CHANGED, dirty=True)

    def _find_gap(self, gap_id: str):
        if not self.page_doc:
            return None
        for g in self.page_doc.gaps:
            if g.gap_id == gap_id:
                return g
        return None

    # ------------------------------------------------------------------
    # Undo / Redo
    # ------------------------------------------------------------------

    def undo(self):
        if not self._undo_stack or not self.page_doc:
            return
        gap_id, old_tag, _ = self._undo_stack.pop()
        self._redo_stack.append((gap_id, old_tag, _))
        # Re-parse the old tag to restore gap state
        gap = self._find_gap(gap_id)
        if gap:
            self._restore_gap_from_tag(gap, old_tag)
        self.notify(EVT_GAP_EDITED, gap_id=gap_id, action="undo")

    def redo(self):
        if not self._redo_stack or not self.page_doc:
            return
        gap_id, _, new_tag = self._redo_stack.pop()
        self._undo_stack.append((gap_id, _, new_tag))
        gap = self._find_gap(gap_id)
        if gap:
            self._restore_gap_from_tag(gap, new_tag)
        self.notify(EVT_GAP_EDITED, gap_id=gap_id, action="redo")

    def _restore_gap_from_tag(self, gap, tag: str):
        from gap_utils import parse_gaps
        parsed = parse_gaps(tag)
        if parsed:
            r = parsed[0]
            gap.status       = r["status"]
            gap.guess        = r["guess"]
            gap.cnf          = r["cnf"]
            gap.user_correction = None   # will be re-derived from tag

    # ------------------------------------------------------------------
    # Word selection
    # ------------------------------------------------------------------

    def select_word(self, word_idx: Optional[int]):
        self.selected_word_idx = word_idx
        self.notify(EVT_WORD_SELECTED, word_idx=word_idx)

    # ------------------------------------------------------------------
    # Mode
    # ------------------------------------------------------------------

    def set_mode(self, mode: str):
        self.mode = mode
        self.notify(EVT_MODE_CHANGED, mode=mode)

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def mark_page_complete(self, notes: str = ""):
        if not self.completion or not self.current_issue:
            return
        stats = self.page_doc.gap_stats() if self.page_doc else {}
        self.completion.mark_page_complete(
            ark_id=self.current_issue["ark_id"],
            page_num=self.page_num,
            remaining_gaps=stats.get("pending", 0),
            notes=notes,
        )
        self.notify(EVT_COMPLETION_CHANGED, page=self.page_num)

    def mark_issue_complete(self):
        if not self.completion or not self.current_issue:
            return
        ark_id = self.current_issue["ark_id"]
        page_statuses = self.completion.all_page_statuses(ark_id)
        pages_done = sum(1 for s in page_statuses.values() if s == "complete")
        self.completion.mark_issue_complete(
            ark_id=ark_id,
            pages_reviewed=pages_done,
        )
        self.notify(EVT_COMPLETION_CHANGED, page=None)

    def is_current_page_complete(self) -> bool:
        if not self.completion or not self.current_issue:
            return False
        return self.completion.is_page_complete(
            self.current_issue["ark_id"], self.page_num)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self):
        if self.page_doc and self.page_doc.is_dirty:
            self.page_doc.save()
            self.notify(EVT_DIRTY_CHANGED, dirty=False)
        if self.doc_map and self.collection_dir:
            self.doc_map.save(self.collection_dir)
