"""
gui/models/completion.py — Page/issue completion manifest.

Stored at {collection}/gui/completion_manifest.json.
A page marked "complete" is skipped by the automated OCR pipeline
(unless --override-complete is passed).
"""

from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_MANIFEST_RELPATH = "gui/completion_manifest.json"


class CompletionManifest:
    """Load, query, and update the completion manifest for a collection."""

    def __init__(self, collection_dir: Path):
        self.collection_dir = Path(collection_dir)
        self.path = self.collection_dir / _MANIFEST_RELPATH
        self._data: dict = {"version": 1, "entries": {}, "issue_complete": {}}
        if self.path.exists():
            self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self):
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {"version": 1, "entries": {}, "issue_complete": {}}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------
    # Page-level API
    # ------------------------------------------------------------------

    def page_key(self, page_num: int) -> str:
        return f"page_{page_num:02d}"

    def page_status(self, ark_id: str, page_num: int) -> str:
        """Return 'complete', 'in_progress', or 'not_started'."""
        entry = (self._data["entries"]
                 .get(ark_id, {})
                 .get(self.page_key(page_num), {}))
        return entry.get("status", "not_started")

    def is_page_complete(self, ark_id: str, page_num: int) -> bool:
        return self.page_status(ark_id, page_num) == "complete"

    def mark_page_complete(self, ark_id: str, page_num: int,
                           remaining_gaps: int = 0, notes: str = ""):
        self._data["entries"].setdefault(ark_id, {})[self.page_key(page_num)] = {
            "status":          "complete",
            "completed_by":    "user",
            "completed_at":    _now_iso(),
            "remaining_gaps":  remaining_gaps,
            "notes":           notes,
        }
        self.save()

    def mark_page_incomplete(self, ark_id: str, page_num: int):
        """Revert a page to in_progress (e.g. user wants to re-edit)."""
        entry = self._data["entries"].get(ark_id, {}).get(self.page_key(page_num))
        if entry:
            entry["status"] = "in_progress"
            entry.pop("completed_at", None)
            self.save()

    def all_page_statuses(self, ark_id: str) -> dict:
        """Return {page_num: status_str} for all known pages of an issue."""
        raw = self._data["entries"].get(ark_id, {})
        result = {}
        for key, entry in raw.items():
            if key.startswith("page_"):
                try:
                    num = int(key[5:])
                    result[num] = entry.get("status", "not_started")
                except ValueError:
                    pass
        return result

    # ------------------------------------------------------------------
    # Issue-level API
    # ------------------------------------------------------------------

    def is_issue_complete(self, ark_id: str) -> bool:
        return (self._data["issue_complete"]
                .get(ark_id, {})
                .get("status") == "complete")

    def mark_issue_complete(self, ark_id: str,
                            pages_reviewed: int = 0,
                            total_gaps_remaining: int = 0,
                            total_gaps_corrected: int = 0):
        self._data["issue_complete"][ark_id] = {
            "status":               "complete",
            "completed_at":         _now_iso(),
            "pages_reviewed":       pages_reviewed,
            "total_gaps_remaining": total_gaps_remaining,
            "total_gaps_corrected": total_gaps_corrected,
        }
        self.save()

    def mark_issue_incomplete(self, ark_id: str):
        entry = self._data["issue_complete"].get(ark_id)
        if entry:
            entry["status"] = "in_progress"
            entry.pop("completed_at", None)
            self.save()

    def issue_summary(self, ark_id: str) -> dict:
        return self._data["issue_complete"].get(ark_id, {})


# ---------------------------------------------------------------------------
# Module-level helper used by the pipeline completion gate
# ---------------------------------------------------------------------------

def is_page_complete(collection_dir: Path, ark_id: str, page_num: int) -> bool:
    """Convenience: load manifest and check a single page. Returns False if
    the manifest does not exist (no GUI interaction yet)."""
    manifest_path = Path(collection_dir) / _MANIFEST_RELPATH
    if not manifest_path.exists():
        return False
    m = CompletionManifest(collection_dir)
    return m.is_page_complete(ark_id, page_num)


def is_issue_complete(collection_dir: Path, ark_id: str) -> bool:
    manifest_path = Path(collection_dir) / _MANIFEST_RELPATH
    if not manifest_path.exists():
        return False
    m = CompletionManifest(collection_dir)
    return m.is_issue_complete(ark_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
