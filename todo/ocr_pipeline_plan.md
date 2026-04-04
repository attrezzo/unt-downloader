# Batch-Aware OCR Preprocessing & Inference Pipeline  
### Iterative Implementation Plan (CLAUDE.md–Compliant)

## 1. Project Goal

Build a two-pass, batch-aware OCR preprocessing pipeline for degraded historical
newspapers that:
1. Extracts high-confidence text signals across a batch of chronologically related issues  
2. Learns typeface, layout, and degradation patterns  
3. Uses that information to recover low-confidence regions  
4. Produces OCR-optimized outputs  
5. Iterates with human-in-the-loop validation  

**This pipeline extends the existing unt_ocr_correct.py — it does NOT replace it.**

## 2. Core Design Principles

1. **Iterative development only** — stop after each phase for validation  
2. **Precision-first approach** — conservative pseudo-labeling, strict gates  
3. **Batch-aware learning** — nearby issues inform preprocessing parameters  
4. **Separation of concerns** — new module, doesn't modify existing pipeline  
5. **Artifact-first design** — every stage persists intermediate outputs  
6. **Use existing tools when possible** — reuse existing image cache, OCR engines  

## 3. CLAUDE.md Compliance Checklist

These rules from CLAUDE.md MUST be satisfied by every phase:

- [ ] **Never discard per-word confidence data.** Store 0–100 integers, never
      flatten to boolean. This is the mechanism for `--reprocess-low-confidence`.
- [ ] **`[unleserlich]` is the only unintelligible marker.** Use exactly
      `ILLEGIBLE = "[unleserlich]"` for zero-confidence words.
- [ ] **Engine-agnosticism.** New engines slot in via the word-token interface:
      `{text, conf, source, left, top, right, bottom}`. Do not couple logic to
      a specific engine.
- [ ] **Never break `--resume`.** All steps must be resumable and idempotent.
      If output formats change, update resume detection.
- [ ] **`confidence/` lives at the collection root** — not inside `artifacts/`.
      Per CLAUDE.md collection directory layout.
- [ ] **No hardcoded `expected_cols=5` at call sites.** Always read from
      collection.json. The default fallback in config is acceptable.
- [ ] **No unnecessary dependencies.** Only use what's already in
      requirements.txt: `numpy`, `scipy`, `opencv-python-headless`, `pillow`,
      `pytesseract`. Do NOT add `pandas`, `networkx`, `faiss`, `pydantic`,
      `scikit-image`, `matplotlib`, or `seaborn` unless a phase explicitly
      requires them AND they're justified.
- [ ] **Do not add `requests` to pipeline modules.** Use only stdlib
      `urllib.request` if network access is ever needed (but this module
      should not need it — images come from existing cache).
- [ ] **OCR engines catch all exceptions and return `[]` on error.** A failed
      engine degrades quality without halting the pipeline.
- [ ] **Document-type flexibility.** Driven by `collection.json` fields
      (`language`, `typeface`, `layout_type`), not hardcoded assumptions.

## 4. Recommended Toolchain

### Python Libraries (already in requirements.txt — no new deps)
- opencv-python-headless  
- numpy  
- scipy  
- pytesseract  
- Pillow  

### OCR Tools (already configured)
- Tesseract OCR (with deu_frak models)  
- Kraken (optional)  

### NOT Used (removed from original plan)
- ~~pandas~~ — use plain dicts/lists  
- ~~networkx~~ — use simple similarity scoring  
- ~~faiss / sklearn.neighbors~~ — use brute-force similarity for batch sizes < 200  
- ~~pydantic~~ — use stdlib dataclasses  
- ~~matplotlib / seaborn~~ — debug images saved via cv2.imwrite  
- ~~scikit-image~~ — use opencv equivalents  

## 5. Project Structure

```
project_root/
├── ocr_pipeline/
│   ├── __init__.py           # module docs + integration points
│   ├── __main__.py           # python -m ocr_pipeline entry point
│   ├── config.py             # PipelineConfig from collection.json
│   ├── types.py              # dataclasses: PageMeta, StyleSignature, etc.
│   ├── logging_utils.py      # StageTimer, JSONL structured logging
│   ├── artifacts.py          # ArtifactStore (manages persistence)
│   ├── batch_runner.py       # Pass A orchestrator
│   └── stages/
│       ├── ingest.py         # Phase 2: image loading + metadata
│       ├── sweep.py          # Phase 3: illumination, threshold, CC analysis
│       ├── features.py       # Phase 4: char height, stroke width, layout
│       ├── store.py          # Phase 5: confidence store, similarity, calibration
│       └── ocr_probe.py      # Phase 6: Tesseract probe on strong regions
├── {collection}/
│   ├── confidence/           # ← per CLAUDE.md, AT collection root
│   │   └── {ark_id}_page{NN}.json
│   └── artifacts/            # ← pipeline-specific working data
│       ├── pipeline_log.jsonl
│       ├── batch_summary.json
│       ├── style_signatures.json
│       ├── low_confidence/
│       └── debug/
└── tests/
    └── test_ocr_pipeline.py
```

---

# PHASED IMPLEMENTATION (ITERATIVE TODO)

## PHASE 0 — Integration Mapping ✓ COMPLETE
- [x] Identify image entry points: `fetch_page_image()`, local cache at
      `images/{ark_id}/page_{NN}.jpg`
- [x] Identify OCR locations: `process_page()` stages 5-6, word token interface
- [x] Define insertion points:
  - Before `process_page()`: batch-level parameter calibration
  - At `preprocess_image()`: use calibrated CLAHE/threshold params
  - After `split_agree_dispute()`: extract confidence records to `confidence/`
  - At `process_issue()` level: Pass A / Pass B orchestration

## PHASE 1 — Scaffolding ✓ COMPLETE
- [x] Create `ocr_pipeline/` module structure  
- [x] Add structured JSONL logging (`pipeline_log.jsonl`)  
- [x] Add artifact directory management (`ArtifactStore`)
- [x] `confidence/` placed at collection root per CLAUDE.md

## PHASE 2 — Image Ingestion ✓ COMPLETE
- [x] Load grayscale from existing image cache (no downloads)
- [x] Capture metadata as `PageMeta` dataclass  
- [x] Validate image size (≥ 50KB, matching `is_valid_cached_image()`)

## PHASE 3 — High-Confidence Sweep ✓ COMPLETE
- [x] Morphological background estimation for illumination flattening
- [x] Adaptive Gaussian thresholding (replaces simple CLAHE for gradient removal)
- [x] Connected component extraction with area filtering
- [x] Region confidence scoring (10x10 grid per page)

## PHASE 4 — Feature Extraction ✓ COMPLETE
- [x] Character height distribution (median, std)
- [x] Stroke width estimation via distance transform
- [x] Ink/background intensity profiles
- [x] Line spacing estimation
- [x] Column count estimation from component x-distribution
- [x] `StyleSignature` aggregation across pages → per-issue signature

## PHASE 5 — Feature Store ✓ COMPLETE
- [x] Per-page confidence records in `{collection}/confidence/`
- [x] Low-confidence region identification and grouping
- [x] Issue similarity scoring (temporal + visual)
- [x] Preprocessing parameter calibration from batch statistics
- [x] `BatchSummary` computation and persistence

## PHASE 6 — OCR Probe ✓ COMPLETE
- [x] Tesseract probe on high-confidence grid cells only
- [x] Per-word confidence extraction (`ConfidenceRecord`)
- [x] Language detection matching unt_ocr_correct.py priority
- [x] `[unleserlich]` for conf < 10 words
- [x] Exception handling: returns `[]` on any error

STOP → USER VALIDATION  

## PHASE 7 — Low-Confidence Detection
- [ ] Produce per-page binary masks of low-confidence regions
- [ ] Classify low-confidence causes: `disagreement`, `low_conf`, `illegible`,
      `faint`, `bleed_through`, `shadow`
- [ ] Store cause codes in `LowConfidenceRegion.reason`
- [ ] Generate summary: % of page area that is low-confidence

STOP → USER VALIDATION  

## PHASE 8 — Batch-Calibrated Preprocessing
- [ ] Use `calibrate_preproc_params()` output to drive actual preprocessing
- [ ] Hook into `preprocess_image()`: accept `PreprocParams` argument
- [ ] A/B comparison: default params vs calibrated params on sample pages
- [ ] Save comparison artifacts (flattened images, before/after)

Note: This phase modifies `unt_ocr_correct.py` — specifically `preprocess_image()`
and `process_page()` — to accept optional calibrated parameters.

STOP → USER VALIDATION  

## PHASE 9 — Temporal Weighting
- [ ] Compute issue similarity matrix using `issue_similarity()`
- [ ] Weight neighbor issues by combined temporal + visual similarity
- [ ] Derive cluster-level `PreprocParams` from weighted neighbor signatures
- [ ] Validate: do cluster params outperform per-issue params?

STOP → USER VALIDATION  

## PHASE 10 — Second-Pass Refinement (Pass B)
- [ ] For each page, load `LowConfidenceRegion` masks
- [ ] Apply heavy preprocessing ONLY to low-confidence crops:
  - Full illumination normalization
  - Aggressive CLAHE (clip=4.0+)
  - Optional deblurring (only if confidence improves)
- [ ] Re-run OCR on enhanced crops
- [ ] Merge with Pass A high-confidence data
- [ ] This enables the planned `--reprocess-low-confidence` mode

STOP → USER VALIDATION  

## PHASE 11 — Adaptive Learning (FUTURE — ENGINE-AGNOSTIC)
- [ ] IMPORTANT: Per CLAUDE.md principle 2 (engine-agnosticism), any model
      training must NOT couple logic to a specific engine
- [ ] Use high-confidence `ConfidenceRecord` data as pseudo-labels
- [ ] Conservative pseudo-label gate: confidence ≥ 70, multi-engine agreement,
      lexicon plausibility
- [ ] If Kraken fine-tuning is pursued: line-image + transcript pairs from
      `confidence/` data, following word-token interface contract
- [ ] Drift detection: monitor confidence distributions across iterations
- [ ] This phase is research-grade and may require new dependencies

STOP → USER VALIDATION  

## PHASE 12 — OCR Comparison
- [ ] Compare multiple preprocessing variants on same pages
- [ ] Metrics: word-level confidence distribution, agreement rate
- [ ] Select best variant per issue cluster
- [ ] Store selection rationale in `artifacts/`

STOP → USER VALIDATION  

## PHASE 13 — Reporting
- [ ] Generate confidence overlay images (ink colored by confidence)
- [ ] Generate per-issue summary reports (JSON + optional HTML)
- [ ] Aggregate collection-level statistics

STOP → USER VALIDATION  

## PHASE 14 — Configuration
- [ ] Expose key parameters in `collection.json`:
  - `batch_pipeline.bg_block_size`
  - `batch_pipeline.thresh_block_size`
  - `batch_pipeline.hc_gate_confidence`
  - `batch_pipeline.similarity_alpha`
  - `batch_pipeline.temporal_tau_days`
- [ ] All with sensible defaults, overridable per collection

STOP → USER VALIDATION  

## PHASE 15 — Regression Testing
- [ ] Freeze a validation set of page images with known-good OCR
- [ ] Run pipeline on validation set, compare confidence distributions
- [ ] Ensure no regression when parameters change

STOP → USER VALIDATION  

## PHASE 16 — Batch Runner Integration
- [ ] Add `--batch-sweep` flag to `unt_archive_downloader.py`
- [ ] Wire through `run_worker()` to `python -m ocr_pipeline`
- [ ] Ensure `--resume` works: skip pages with existing confidence data
- [ ] Add batch sweep status to `--status` output

STOP → USER VALIDATION  

---

## Human-in-the-Loop Feedback Template

```
Step completed:  
Did it run:  
Errors:  
Artifacts generated:  
What looks right:  
What looks wrong:  
Priority fix:  
```

---

## LLM Control Prompt

Work in small steps.  
Do not implement the full system at once.  
Add one feature at a time with logging and artifacts.  
Stop after each step and wait for validation.  
Preserve existing behavior — do not modify unt_ocr_correct.py without explicit approval.  
Prioritize high-confidence precision.  
Do not trust low-confidence data as ground truth.  
Always use `[unleserlich]` for illegible markers — no other form.  
Store confidence as 0–100 integers, never booleans.  
`confidence/` goes at the collection root, not inside `artifacts/`.  
