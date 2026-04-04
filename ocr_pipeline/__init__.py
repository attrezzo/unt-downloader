"""
ocr_pipeline — Batch-aware OCR preprocessing and confidence analysis.

This module extends the existing unt_ocr_correct.py pipeline with:
  - Batch-wide style and degradation statistics
  - Adaptive preprocessing parameters per issue/cluster
  - High-confidence data extraction and feature storage
  - Targeted refinement of low-confidence regions

It does NOT replace the existing pipeline — it feeds into it by
providing better preprocessing parameters and batch context.

Integration points with unt_ocr_correct.py:
  - Before process_page():  batch-level parameter calibration
  - At preprocess_image():  adaptive CLAHE/threshold parameters
  - After split_agree_dispute(): confidence data extraction
  - At process_issue() level: Pass A / Pass B orchestration
"""
