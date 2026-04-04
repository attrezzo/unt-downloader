"""
tests/test_pipeline.py — Smoke tests for the unt-downloader pipeline modules.

Tests only pure functions that don't require network, API keys, or OCR engines.
"""

import pytest
import sys
from pathlib import Path

# Add project root to path so we can import the production modules
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestRateLimiter:
    """Test claude_rate_limiter.py token-bucket logic."""

    def test_create_default_limiter(self):
        from claude_rate_limiter import ClaudeRateLimiter
        limiter = ClaudeRateLimiter(rpm=50, tpm=40_000)
        assert limiter.total_requests == 0
        assert limiter.total_tokens == 0

    def test_acquire_deducts_from_buckets(self):
        from claude_rate_limiter import ClaudeRateLimiter
        limiter = ClaudeRateLimiter(rpm=50, tpm=40_000)
        limiter.acquire(estimated_tokens=1000)
        assert limiter.total_requests == 1

    def test_record_usage_tracks_tokens(self):
        from claude_rate_limiter import ClaudeRateLimiter
        limiter = ClaudeRateLimiter(rpm=50, tpm=40_000)
        limiter.acquire(estimated_tokens=1000)
        limiter.record_usage(input_tokens=800, output_tokens=200)
        assert limiter.total_tokens == 1000

    def test_limiter_from_tier(self):
        from claude_rate_limiter import limiter_from_tier
        limiter = limiter_from_tier("build")
        # Build tier has higher capacity
        assert limiter._rpm_capacity > 50  # safety_factor applied to 1000

    def test_status_line(self):
        from claude_rate_limiter import ClaudeRateLimiter
        limiter = ClaudeRateLimiter(rpm=50, tpm=40_000)
        line = limiter.status_line()
        assert "RPM bucket" in line
        assert "TPM bucket" in line


class TestCostEstimate:
    """Test unt_cost_estimate.py pricing and utility functions."""

    def test_load_pricing(self):
        from unt_cost_estimate import load_pricing
        pricing = load_pricing()
        # pricing.json should exist and have model entries
        assert isinstance(pricing, dict)

    def test_pricing_meta(self):
        from unt_cost_estimate import pricing_meta
        meta = pricing_meta()
        assert isinstance(meta, dict)

    def test_derive_tier(self):
        from unt_cost_estimate import _derive_tier
        assert _derive_tier("claude-haiku-4-5-20251001") == "haiku"
        assert _derive_tier("claude-sonnet-4-6") == "sonnet"
        assert _derive_tier("claude-opus-4-6") == "opus"
        assert _derive_tier("unknown-model") == "unknown"

    def test_derive_display_name(self):
        from unt_cost_estimate import _derive_display_name
        name = _derive_display_name("claude-sonnet-4-6")
        assert "Sonnet" in name


class TestTranslateAudit:
    """Test unt_translate.py pure functions."""

    def test_is_untranslated_budget_exceeded(self):
        from unt_translate import _is_untranslated_content
        assert _is_untranslated_content("[BUDGET EXCEEDED: PAGE 3]")
        assert _is_untranslated_content("[TRANSLATION FAILED: PAGE 1]")
        assert _is_untranslated_content("[NO SOURCE TEXT: PAGE 2]")
        assert _is_untranslated_content("")

    def test_is_untranslated_html(self):
        from unt_translate import _is_untranslated_content
        assert _is_untranslated_content("<!DOCTYPE html><html>...")
        assert _is_untranslated_content("<html><body>text</body></html>")

    def test_good_content_is_not_untranslated(self):
        from unt_translate import _is_untranslated_content
        assert not _is_untranslated_content("This is a normal translated paragraph.")

    def test_parse_pages(self):
        from unt_translate import parse_pages
        text = (
            "=== TEST TITLE ===\n"
            "ARK:    metapth1234567\n"
            "============================================================\n"
            "\n"
            "--- Page 1 of 2 ---\n"
            "First page content.\n"
            "\n"
            "--- Page 2 of 2 ---\n"
            "Second page content.\n"
        )
        header, pages = parse_pages(text)
        assert "TEST TITLE" in header
        assert 1 in pages
        assert 2 in pages
        assert "First page" in pages[1]
        assert "Second page" in pages[2]


class TestOcrCorrectPureFunctions:
    """Test unt_ocr_correct.py functions that don't need OpenCV/Tesseract."""

    def test_strip_ocr_html_plain_text(self):
        from unt_ocr_correct import strip_ocr_html
        # Plain text should pass through unchanged
        plain = "This is plain text.\nLine two."
        assert strip_ocr_html(plain) == plain

    def test_strip_ocr_html_removes_tags(self):
        from unt_ocr_correct import strip_ocr_html
        html = "<p>Some <b>bold</b> text</p>"
        result = strip_ocr_html(html)
        assert "<p>" not in result
        assert "<b>" not in result

    def test_parse_ocr_pages(self):
        from unt_ocr_correct import parse_ocr_pages
        text = (
            "=== COLLECTION ===\n"
            "ARK:    metapth1234567\n"
            "============================================================\n"
            "\n"
            "--- Page 1 of 2 ---\n"
            "Page one text.\n"
            "\n"
            "--- Page 2 of 2 ---\n"
            "Page two text.\n"
        )
        header, pages = parse_ocr_pages(text)
        assert 1 in pages
        assert 2 in pages
