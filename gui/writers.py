"""
gui/writers.py — Atomic writes for all GUI-produced data.

All writes use write-to-tmp + rename for crash safety (POSIX atomic).
This module is the ONLY place that writes to collection files from the GUI.
"""

from __future__ import annotations
import json
from pathlib import Path

from .models.page_document import PageDocument
from .models.completion import CompletionManifest
from .models.map_file import DocumentMap


def save_page(doc: PageDocument):
    """Write edited page text back to ai_ocr/.md (atomic)."""
    doc.save()


def save_completion(manifest: CompletionManifest):
    """Persist the completion manifest (atomic, handled internally)."""
    manifest.save()


def save_document_map(doc_map: DocumentMap, collection_dir: Path):
    """Persist a document map (atomic, handled internally)."""
    doc_map.save(collection_dir)


def atomic_write(path: Path, content: str, encoding: str = "utf-8"):
    """Generic atomic text write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding=encoding)
    tmp.replace(path)


def atomic_write_json(path: Path, data, indent: int = 2):
    """Generic atomic JSON write."""
    atomic_write(path, json.dumps(data, indent=indent, ensure_ascii=False))
