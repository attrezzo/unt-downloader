"""
gui/loaders.py — Read collection/issue/page data from the pipeline's directory layout.

Does NOT import from unt_ocr_correct.py (avoids module-level globals and side effects).
Imports from ocr_pipeline/ package and gap_utils only.
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from typing import Optional

from .models.page_document import PageDocument
from .models.completion import CompletionManifest
from .models.map_file import DocumentMap
from .models.char_measure import CharDefaults


# ---------------------------------------------------------------------------
# Collection loading
# ---------------------------------------------------------------------------

def load_collection_json(collection_dir: Path) -> dict:
    """Load and return collection.json."""
    p = Path(collection_dir) / "collection.json"
    if not p.exists():
        raise FileNotFoundError(f"collection.json not found in {collection_dir}")
    return json.loads(p.read_text(encoding="utf-8"))


def load_issues(collection_dir: Path) -> list:
    """Load all_issues.json and return list of issue dicts, sorted by date."""
    p = Path(collection_dir) / "metadata" / "all_issues.json"
    if not p.exists():
        return []
    issues = json.loads(p.read_text(encoding="utf-8"))
    return sorted(issues, key=lambda i: i.get("date", ""))


def load_global_config(collection_dir: Optional[Path] = None) -> dict:
    """Load config.json from the repo root (walk up from collection_dir or CWD)."""
    search_dirs = []
    if collection_dir:
        search_dirs.append(Path(collection_dir))
        search_dirs.extend(Path(collection_dir).parents)
    search_dirs.extend(Path.cwd().parents)
    search_dirs.insert(0, Path.cwd())

    for d in search_dirs:
        p = d / "config.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


# ---------------------------------------------------------------------------
# Issue helpers
# ---------------------------------------------------------------------------

def issue_filename(issue: dict) -> str:
    """Build the standard filename stem for an issue."""
    ark_id = issue["ark_id"]
    vol    = str(issue.get("volume", "?")).zfill(2)
    num    = str(issue.get("number", "?")).zfill(2)
    date   = re.sub(r"[^\w\-]", "-", issue.get("date", "unknown"))
    return f"{ark_id}_vol{vol}_no{num}_{date}"


def page_count_for_issue(issue: dict, collection_dir: Path) -> int:
    """Return the number of pages for this issue (from images/ or metadata)."""
    ark_id  = issue["ark_id"]
    img_dir = Path(collection_dir) / "images" / ark_id
    if img_dir.exists():
        jpgs = sorted(img_dir.glob("page_*.jp*g"))
        if jpgs:
            return len(jpgs)
    return int(issue.get("pages", 8))


def has_image(issue: dict, page_num: int, collection_dir: Path) -> bool:
    ark_id  = issue["ark_id"]
    img_dir = Path(collection_dir) / "images" / ark_id
    return any([
        (img_dir / f"page_{page_num:02d}.jpg").exists(),
        (img_dir / f"page_{page_num:02d}.jpeg").exists(),
        (img_dir / f"page_{page_num}.jpg").exists(),
    ])


def has_ai_ocr(issue: dict, page_num: int, collection_dir: Path) -> bool:
    ark_id = issue["ark_id"]
    return (Path(collection_dir) / "ai_ocr" / ark_id /
            f"page_{page_num:02d}.md").exists()


# ---------------------------------------------------------------------------
# Page loading
# ---------------------------------------------------------------------------

def load_page(issue: dict, page_num: int, collection_dir: Path) -> PageDocument:
    """Load all data for one page and return a PageDocument."""
    total = page_count_for_issue(issue, collection_dir)
    return PageDocument.load(
        ark_id=issue["ark_id"],
        page_num=page_num,
        total_pages=total,
        collection_dir=collection_dir,
    )


# ---------------------------------------------------------------------------
# Style signature / char defaults
# ---------------------------------------------------------------------------

def load_char_defaults(ark_id: str, collection_dir: Path) -> CharDefaults:
    """Load character defaults from style_signatures.json for this issue."""
    path = Path(collection_dir) / "artifacts" / "style_signatures.json"
    if not path.exists():
        return CharDefaults()
    sigs = json.loads(path.read_text(encoding="utf-8"))
    for s in sigs:
        if s.get("ark_id", "").startswith(ark_id[:8]):
            return CharDefaults.from_style_signature(s)
    if sigs:
        return CharDefaults.from_style_signature(sigs[0])
    return CharDefaults()


# ---------------------------------------------------------------------------
# Completion manifest (thin re-export so callers only need to import loaders)
# ---------------------------------------------------------------------------

def load_completion(collection_dir: Path) -> CompletionManifest:
    return CompletionManifest(Path(collection_dir))


def load_document_map(ark_id: str, page_num: int,
                      collection_dir: Path) -> DocumentMap:
    return DocumentMap.load(Path(collection_dir), ark_id, page_num)
