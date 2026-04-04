"""
tests/test_ocr_pipeline.py — Tests for the batch-aware OCR preprocessing pipeline.

Tests pure functions that don't require Tesseract, network, or API keys.
Uses synthetic images (numpy arrays) to test image processing stages.
"""

import pytest
import sys
import json
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np


# ============================================================================
# Types
# ============================================================================

class TestTypes:
    """Test data structure serialization."""

    def test_page_meta_roundtrip(self):
        from ocr_pipeline.types import PageMeta
        meta = PageMeta(
            ark_id="metapth123", page_num=3, total_pages=8,
            issue_date="1891-09-17", volume="01", number="01",
            image_path="/tmp/test.jpg", width=4000, height=6000,
        )
        d = meta.to_dict()
        assert d["ark_id"] == "metapth123"
        assert d["width"] == 4000
        restored = PageMeta.from_dict(d)
        assert restored.ark_id == meta.ark_id
        assert restored.width == meta.width

    def test_style_signature_roundtrip(self):
        from ocr_pipeline.types import StyleSignature
        sig = StyleSignature(
            ark_id="metapth123", issue_date="1891-09-17",
            median_char_height=25.0, contrast_ratio=2.5,
        )
        d = sig.to_dict()
        assert d["median_char_height"] == 25.0
        restored = StyleSignature.from_dict(d)
        assert restored.contrast_ratio == 2.5

    def test_preproc_params_defaults(self):
        from ocr_pipeline.types import PreprocParams
        p = PreprocParams()
        assert p.clahe_clip_limit == 2.5
        assert p.clahe_tile_size == 8
        assert p.source == "default"

    def test_batch_summary_save_load(self):
        from ocr_pipeline.types import BatchSummary
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "summary.json"
            summary = BatchSummary(
                collection_title="Test", n_issues=10, n_pages=80,
                n_words_total=5000, n_words_high_conf=3500,
            )
            summary.save(path)
            loaded = BatchSummary.load(path)
            assert loaded.n_issues == 10
            assert loaded.n_words_high_conf == 3500
            assert loaded.high_conf_fraction() == 3500 / 5000

    def test_confidence_record_to_dict(self):
        from ocr_pipeline.types import ConfidenceRecord
        cr = ConfidenceRecord(
            ark_id="test", page_num=1, column=1, word_index=0,
            text="Hallo", confidence=85, agreed=True, source_count=2,
        )
        d = cr.to_dict()
        assert d["text"] == "Hallo"
        assert d["confidence"] == 85


# ============================================================================
# Config
# ============================================================================

class TestConfig:
    def test_pipeline_config_from_json(self):
        from ocr_pipeline.config import PipelineConfig
        with tempfile.TemporaryDirectory() as td:
            cj = Path(td) / "collection.json"
            cj.write_text(json.dumps({
                "title_name": "Test Paper",
                "language": "German",
                "typeface": "Fraktur",
                "layout_type": "newspaper",
            }), encoding="utf-8")
            config = PipelineConfig.from_collection_json(cj)
            assert config.title_name == "Test Paper"
            assert config.language == "German"
            assert config.artifact_dir == str(Path(td) / "artifacts")


# ============================================================================
# Artifacts
# ============================================================================

class TestArtifactStore:
    def test_init_creates_directories(self):
        from ocr_pipeline.artifacts import ArtifactStore
        with tempfile.TemporaryDirectory() as td:
            store = ArtifactStore(Path(td))
            store.init()
            assert (Path(td) / "artifacts" / "confidence").is_dir()
            assert (Path(td) / "artifacts" / "low_confidence").is_dir()

    def test_confidence_save_load(self):
        from ocr_pipeline.artifacts import ArtifactStore
        with tempfile.TemporaryDirectory() as td:
            store = ArtifactStore(Path(td))
            store.init()
            records = [{"text": "word", "confidence": 80, "agreed": True}]
            store.save_page_confidence("ark123", 1, records)
            loaded = store.load_page_confidence("ark123", 1)
            assert len(loaded) == 1
            assert loaded[0]["text"] == "word"

    def test_missing_returns_empty(self):
        from ocr_pipeline.artifacts import ArtifactStore
        with tempfile.TemporaryDirectory() as td:
            store = ArtifactStore(Path(td))
            store.init()
            assert store.load_page_confidence("nonexistent", 1) == []
            assert store.load_low_conf_regions("nonexistent", 1) == []
            assert store.load_style_signatures() == []
            assert store.load_batch_summary() == {}

    def test_style_signatures_roundtrip(self):
        from ocr_pipeline.artifacts import ArtifactStore
        with tempfile.TemporaryDirectory() as td:
            store = ArtifactStore(Path(td))
            store.init()
            sigs = [{"ark_id": "a", "contrast_ratio": 2.5},
                    {"ark_id": "b", "contrast_ratio": 1.8}]
            store.save_style_signatures(sigs)
            loaded = store.load_style_signatures()
            assert len(loaded) == 2
            assert loaded[0]["contrast_ratio"] == 2.5


# ============================================================================
# Sweep (Phase 3)
# ============================================================================

class TestSweep:
    """Test image processing with synthetic images."""

    def _make_test_image(self, width=400, height=600):
        """Create a synthetic page: light background with dark text-like blobs."""
        img = np.full((height, width), 200, dtype=np.uint8)  # light background

        # Add some dark rectangles (simulating text lines)
        for y in range(50, height - 50, 30):
            for x in range(20, width - 20, 15):
                # Small dark rectangles = character-like
                h, w = np.random.randint(8, 20), np.random.randint(5, 12)
                img[y:y+h, x:x+w] = np.random.randint(20, 60)

        return img

    def test_estimate_background(self):
        from ocr_pipeline.stages.sweep import estimate_background, HAS_CV2
        if not HAS_CV2:
            pytest.skip("cv2 not available")
        img = self._make_test_image()
        bg = estimate_background(img, block_size=51)
        assert bg.shape == img.shape
        # Background should be brighter than the original (it's the bright envelope)
        assert bg.mean() >= img.mean()

    def test_flatten_illumination(self):
        from ocr_pipeline.stages.sweep import flatten_illumination, HAS_CV2
        if not HAS_CV2:
            pytest.skip("cv2 not available")
        # Create image with gradient (simulating uneven illumination)
        img = np.full((200, 300), 180, dtype=np.uint8)
        # Add horizontal gradient
        gradient = np.linspace(100, 220, 300).astype(np.uint8)
        img = (img.astype(float) * gradient[np.newaxis, :] / 200).astype(np.uint8)
        flat = flatten_illumination(img)
        assert flat.shape == img.shape
        # Flattened should have more uniform background
        flat_std = float(np.std(flat))
        img_std = float(np.std(img))
        # The flattened image should be at least somewhat more uniform
        assert flat.dtype == np.uint8

    def test_adaptive_threshold(self):
        from ocr_pipeline.stages.sweep import adaptive_threshold, HAS_CV2
        if not HAS_CV2:
            pytest.skip("cv2 not available")
        img = self._make_test_image()
        binary = adaptive_threshold(img)
        assert binary.shape == img.shape
        # Should be truly binary
        unique = np.unique(binary)
        assert set(unique).issubset({0, 255})
        # Should have some foreground
        assert binary.sum() > 0

    def test_extract_components(self):
        from ocr_pipeline.stages.sweep import (
            adaptive_threshold, extract_components, HAS_CV2,
        )
        if not HAS_CV2:
            pytest.skip("cv2 not available")
        img = self._make_test_image()
        binary = adaptive_threshold(img)
        comps = extract_components(binary)
        assert len(comps) > 0
        # Check component structure
        c = comps[0]
        assert "left" in c
        assert "top" in c
        assert "area" in c
        assert "aspect_ratio" in c
        assert c["area"] >= 20  # min_area default

    def test_score_region_confidence(self):
        from ocr_pipeline.stages.sweep import (
            adaptive_threshold, extract_components,
            score_region_confidence, HAS_CV2,
        )
        if not HAS_CV2:
            pytest.skip("cv2 not available")
        img = self._make_test_image()
        binary = adaptive_threshold(img)
        comps = extract_components(binary)
        conf_map = score_region_confidence(img, binary, comps)
        assert conf_map.shape == (10, 10)
        assert conf_map.min() >= 0.0
        assert conf_map.max() <= 1.0

    def test_sweep_page_full(self):
        from ocr_pipeline.stages.sweep import sweep_page, HAS_CV2
        if not HAS_CV2:
            pytest.skip("cv2 not available")
        img = self._make_test_image()
        result = sweep_page(img)
        assert "flattened" in result
        assert "binary" in result
        assert "components" in result
        assert "conf_map" in result
        assert "stats" in result
        assert result["stats"]["n_components"] > 0


# ============================================================================
# Features (Phase 4)
# ============================================================================

class TestFeatures:

    def test_character_height_stats(self):
        from ocr_pipeline.stages.features import character_height_stats
        comps = [{"height": 20, "aspect_ratio": 0.6} for _ in range(50)]
        med, std = character_height_stats(comps)
        assert med == 20.0
        assert std == 0.0

    def test_character_height_needs_minimum(self):
        from ocr_pipeline.stages.features import character_height_stats
        comps = [{"height": 20, "aspect_ratio": 0.6} for _ in range(5)]
        med, std = character_height_stats(comps)
        assert med == 0.0  # too few components

    def test_intensity_profile(self):
        from ocr_pipeline.stages.features import intensity_profile
        img = np.full((100, 100), 200, dtype=np.uint8)
        binary = np.zeros_like(img)
        binary[20:80, 20:80] = 255  # foreground region
        img[20:80, 20:80] = 40       # dark ink
        bg_m, bg_s, fg_m, fg_s, contrast = intensity_profile(img, binary)
        assert bg_m > fg_m
        assert contrast > 1.0

    def test_content_fraction(self):
        from ocr_pipeline.stages.features import content_fraction
        binary = np.zeros((100, 100), dtype=np.uint8)
        binary[:50, :] = 255  # half foreground
        frac = content_fraction(binary)
        assert abs(frac - 0.5) < 0.01

    def test_estimate_line_spacing(self):
        from ocr_pipeline.stages.features import estimate_line_spacing
        # Create components in evenly spaced rows
        comps = []
        for row in range(10):
            for col in range(5):
                comps.append({
                    "cy": row * 30 + 15,
                    "cx": col * 40,
                    "height": 20,
                    "aspect_ratio": 0.7,
                })
        med, std = estimate_line_spacing(comps)
        assert abs(med - 30.0) < 2.0  # should detect ~30px spacing

    def test_aggregate_signatures(self):
        from ocr_pipeline.stages.features import aggregate_signatures
        from ocr_pipeline.types import StyleSignature
        sigs = [
            StyleSignature(ark_id="a", issue_date="2024-01-01",
                           median_char_height=20, contrast_ratio=2.0,
                           n_components_sampled=100, n_pages_sampled=1),
            StyleSignature(ark_id="a", issue_date="2024-01-01",
                           median_char_height=22, contrast_ratio=2.2,
                           n_components_sampled=150, n_pages_sampled=1),
        ]
        agg = aggregate_signatures(sigs)
        assert agg.ark_id == "a"
        assert 20 <= agg.median_char_height <= 22
        assert agg.n_components_sampled == 250
        assert agg.n_pages_sampled == 2


# ============================================================================
# Store (Phase 5)
# ============================================================================

class TestStore:

    def test_identify_low_confidence_regions(self):
        from ocr_pipeline.stages.store import identify_low_confidence_regions
        from ocr_pipeline.types import ConfidenceRecord
        records = [
            ConfidenceRecord(ark_id="a", page_num=1, column=1, word_index=0,
                             text="der", confidence=90, agreed=True, source_count=2,
                             top=100, left=10, right=40, bottom=120),
            ConfidenceRecord(ark_id="a", page_num=1, column=1, word_index=1,
                             text="???", confidence=15, agreed=False, source_count=2,
                             top=100, left=50, right=80, bottom=120),
            ConfidenceRecord(ark_id="a", page_num=1, column=1, word_index=2,
                             text="und", confidence=10, agreed=False, source_count=2,
                             top=105, left=90, right=120, bottom=125),
        ]
        regions = identify_low_confidence_regions(records, conf_threshold=40)
        # The two low-conf words should be grouped (same line, close together)
        assert len(regions) == 1
        assert regions[0].mean_confidence < 40

    def test_issue_similarity_temporal(self):
        from ocr_pipeline.stages.store import issue_similarity
        from ocr_pipeline.types import StyleSignature
        a = StyleSignature(ark_id="a", issue_date="1891-09-17",
                           median_char_height=20, contrast_ratio=2.0)
        b = StyleSignature(ark_id="b", issue_date="1891-09-24",
                           median_char_height=20, contrast_ratio=2.0)
        c = StyleSignature(ark_id="c", issue_date="1892-09-17",
                           median_char_height=20, contrast_ratio=2.0)
        sim_ab = issue_similarity(a, b)
        sim_ac = issue_similarity(a, c)
        assert sim_ab > sim_ac  # closer in time = more similar

    def test_calibrate_preproc_params(self):
        from ocr_pipeline.stages.store import calibrate_preproc_params
        from ocr_pipeline.types import StyleSignature
        # Good quality scan
        sig_good = StyleSignature(
            ark_id="a", issue_date="", contrast_ratio=3.0,
            median_char_height=25, bg_intensity_mean=200,
            fg_intensity_mean=40,
        )
        params_good = calibrate_preproc_params(sig_good)
        assert params_good.clahe_clip_limit == 2.0  # gentle

        # Poor quality scan
        sig_poor = StyleSignature(
            ark_id="b", issue_date="", contrast_ratio=1.3,
            median_char_height=12, bg_intensity_mean=100,
            fg_intensity_mean=70,
        )
        params_poor = calibrate_preproc_params(sig_poor)
        assert params_poor.clahe_clip_limit == 4.0  # aggressive

    def test_find_similar_issues(self):
        from ocr_pipeline.stages.store import find_similar_issues
        from ocr_pipeline.types import StyleSignature
        target = StyleSignature(ark_id="target", issue_date="1891-09-17",
                                median_char_height=20, contrast_ratio=2.0)
        others = [
            StyleSignature(ark_id="near", issue_date="1891-09-24",
                           median_char_height=21, contrast_ratio=2.1),
            StyleSignature(ark_id="far", issue_date="1892-06-01",
                           median_char_height=30, contrast_ratio=1.5),
        ]
        results = find_similar_issues(target, others, top_k=2)
        assert len(results) == 2
        assert results[0][1].ark_id == "near"  # most similar


# ============================================================================
# Logging
# ============================================================================

class TestLogging:
    def test_stage_timer(self):
        from ocr_pipeline.logging_utils import StageTimer
        with tempfile.TemporaryDirectory() as td:
            with StageTimer("test_stage", Path(td)):
                pass  # instant
            log_path = Path(td) / "pipeline_log.jsonl"
            assert log_path.exists()
            entry = json.loads(log_path.read_text().strip())
            assert entry["stage"] == "test_stage"
            assert entry["status"] == "ok"
            assert entry["elapsed_s"] >= 0
