"""
ocr_pipeline.artifacts — Artifact storage for pipeline stages.

Confidence data lives at the COLLECTION root per CLAUDE.md:
  {collection}/confidence/          per-word confidence sidecar data

Pipeline-specific artifacts live under {collection}/artifacts/:
  artifacts/
    pipeline_log.jsonl              structured stage log (append-only)
    batch_summary.json              aggregate stats from latest sweep
    style_signatures.json           per-issue style data
    low_confidence/
      {ark_id}_page{NN}.json        flagged regions for Pass B
    debug/
      {timestamp}/
        {ark_id}_p{NN}_*.png        debug images (when enabled)
"""

import json
import time
from pathlib import Path
from typing import Optional


class ArtifactStore:
    """
    Manages the artifacts/ directory for a collection.
    Handles creation, path resolution, and persistence of pipeline data.
    """

    def __init__(self, collection_dir: Path):
        self.collection_dir = collection_dir
        self.root = collection_dir / "artifacts"
        # confidence/ lives at collection root per CLAUDE.md collection layout
        self.confidence_dir = collection_dir / "confidence"
        self.low_conf_dir = self.root / "low_confidence"
        self.debug_dir = self.root / "debug"
        self._debug_session: Optional[Path] = None

    def init(self):
        """Create artifact directories. Call once at pipeline start."""
        for d in [self.root, self.confidence_dir, self.low_conf_dir]:
            d.mkdir(parents=True, exist_ok=True)

    def debug_session_dir(self) -> Path:
        """Get or create a timestamped debug directory for this run."""
        if self._debug_session is None:
            ts = time.strftime("%Y%m%d_%H%M%S")
            self._debug_session = self.debug_dir / ts
            self._debug_session.mkdir(parents=True, exist_ok=True)
        return self._debug_session

    # ── Confidence records ────────────────────────────────────────────────

    def save_page_confidence(self, ark_id: str, page_num: int, records: list):
        """Save per-word confidence records for one page."""
        path = self.confidence_dir / f"{ark_id}_page{page_num:02d}.json"
        path.write_text(
            json.dumps([r if isinstance(r, dict) else r.to_dict() for r in records],
                       indent=1),
            encoding="utf-8")

    def load_page_confidence(self, ark_id: str, page_num: int) -> list:
        """Load confidence records for one page. Returns [] if absent."""
        path = self.confidence_dir / f"{ark_id}_page{page_num:02d}.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    # ── Low-confidence regions ────────────────────────────────────────────

    def save_low_conf_regions(self, ark_id: str, page_num: int, regions: list):
        """Save low-confidence regions for one page."""
        path = self.low_conf_dir / f"{ark_id}_page{page_num:02d}.json"
        path.write_text(
            json.dumps([r if isinstance(r, dict) else r.to_dict() for r in regions],
                       indent=1),
            encoding="utf-8")

    def load_low_conf_regions(self, ark_id: str, page_num: int) -> list:
        """Load low-confidence regions. Returns [] if absent."""
        path = self.low_conf_dir / f"{ark_id}_page{page_num:02d}.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    # ── Style signatures ──────────────────────────────────────────────────

    def save_style_signatures(self, signatures: list):
        """Save all issue style signatures."""
        path = self.root / "style_signatures.json"
        data = [s if isinstance(s, dict) else s.to_dict() for s in signatures]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_style_signatures(self) -> list:
        """Load style signatures. Returns [] if absent."""
        path = self.root / "style_signatures.json"
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    # ── Batch summary ─────────────────────────────────────────────────────

    def save_batch_summary(self, summary):
        """Save batch-level aggregate summary."""
        path = self.root / "batch_summary.json"
        data = summary if isinstance(summary, dict) else summary.to_dict()
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def load_batch_summary(self) -> dict:
        """Load batch summary. Returns {} if absent."""
        path = self.root / "batch_summary.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    # ── Debug images ──────────────────────────────────────────────────────

    def save_debug_image(self, img, filename: str):
        """Save a debug image (numpy array) to the current debug session."""
        try:
            import cv2
            out = self.debug_session_dir() / filename
            cv2.imwrite(str(out), img)
            return out
        except ImportError:
            return None
