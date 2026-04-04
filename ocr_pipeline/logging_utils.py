"""
ocr_pipeline.logging_utils — Structured logging for pipeline stages.

Each stage logs to both console (via tprint from unt_ocr_correct) and
a structured JSON log in the artifacts directory.
"""

import json
import time
import threading
from pathlib import Path

_log_lock = threading.Lock()
_print_lock = threading.Lock()


def pipeline_log(msg: str, level: str = "info", worker: str = ""):
    """Thread-safe console print, matching unt_ocr_correct.tprint style."""
    with _print_lock:
        prefix = f"[{worker}] " if worker else ""
        tag = f"[{level.upper()}]" if level != "info" else ""
        parts = [p for p in [prefix, tag, msg] if p]
        print(" ".join(parts), flush=True)


def append_stage_log(artifact_dir: Path, entry: dict):
    """Append a structured log entry to artifacts/pipeline_log.jsonl."""
    log_path = artifact_dir / "pipeline_log.jsonl"
    entry["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _log_lock:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


class StageTimer:
    """Context manager for timing pipeline stages."""

    def __init__(self, stage_name: str, artifact_dir: Path = None, **extra):
        self.stage_name = stage_name
        self.artifact_dir = artifact_dir
        self.extra = extra
        self.start = 0.0
        self.elapsed = 0.0

    def __enter__(self):
        self.start = time.monotonic()
        pipeline_log(f"  {self.stage_name} ...", level="info")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.elapsed = time.monotonic() - self.start
        status = "ok" if exc_type is None else f"error: {exc_val}"
        pipeline_log(
            f"  {self.stage_name} [{self.elapsed:.1f}s] {status}",
            level="info" if exc_type is None else "error",
        )
        if self.artifact_dir:
            append_stage_log(self.artifact_dir, {
                "stage": self.stage_name,
                "elapsed_s": round(self.elapsed, 2),
                "status": status,
                **self.extra,
            })
        return False  # don't suppress exceptions
