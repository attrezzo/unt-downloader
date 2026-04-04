"""
ocr_pipeline.stages — Individual pipeline stages.

Each stage is a module with a clear input/output contract:
  ingest.py       — Phase 2: image loading + metadata capture
  sweep.py        — Phase 3: high-confidence sweep (illumination, threshold, CC)
  features.py     — Phase 4: feature extraction (char size, stroke width, layout)
  store.py        — Phase 5: feature store (persist + lookup)
  ocr_probe.py    — Phase 6: OCR on strong regions
"""
