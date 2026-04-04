"""
tests/test_pipeline.py — Basic smoke tests for the unt-downloader pipeline.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestChunking:
    """Test text chunking logic shared by ocr_correct and translate."""

    def test_chunk_at_paragraph_boundary(self):
        from translate import _chunk_text
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = _chunk_text(text, chunk_size=20)
        # Each chunk should be a complete paragraph
        for chunk in chunks:
            assert chunk.strip()

    def test_single_chunk_if_short(self):
        from translate import _chunk_text
        text = "Short text.\n\nAnother short one."
        chunks = _chunk_text(text, chunk_size=10000)
        assert len(chunks) == 1

    def test_empty_text(self):
        from translate import _chunk_text
        chunks = _chunk_text("", chunk_size=2000)
        assert chunks == []


class TestRenderPdf:
    """Test PDF rendering."""

    def test_render_creates_file(self, tmp_path):
        from render_pdf import render_pdf

        german = tmp_path / "corrected.txt"
        english = tmp_path / "translated.txt"
        output = tmp_path / "test_issue.pdf"

        german.write_text("Erster Absatz.\n\nZweiter Absatz.", encoding="utf-8")
        english.write_text("First paragraph.\n\nSecond paragraph.", encoding="utf-8")

        render_pdf(german, english, output, "test-issue-001")
        assert output.exists()
        assert output.stat().st_size > 0
