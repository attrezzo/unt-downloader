"""
ocr_pipeline.batch_runner — Orchestrates the batch-aware OCR preprocessing pipeline.

This is the main entry point for the batch pipeline. It runs Pass A
(fast sweep) across all issues in a collection, extracting style
signatures, confidence data, and low-confidence region maps.

Usage:
    python -m ocr_pipeline.batch_runner --config-path bellville_wochenblatt/collection.json
    python -m ocr_pipeline.batch_runner --config-path collection.json --ark metapth1478562
    python -m ocr_pipeline.batch_runner --config-path collection.json --save-debug-images

Pass A outputs (in artifacts/):
    - style_signatures.json       per-issue typography/degradation profiles
    - batch_summary.json          aggregate statistics
    - confidence/{ark}_page{NN}.json   per-word confidence data
    - low_confidence/{ark}_page{NN}.json   regions flagged for Pass B
    - pipeline_log.jsonl          structured stage log
    - debug/{timestamp}/*.png     debug images (when enabled)
"""

import argparse
import json
import sys
import os
from pathlib import Path

import numpy as np

from ocr_pipeline.config import PipelineConfig
from ocr_pipeline.artifacts import ArtifactStore
from ocr_pipeline.logging_utils import pipeline_log, StageTimer, append_stage_log
from ocr_pipeline.stages.ingest import (
    ingest_page, load_issue_index, image_path, is_valid_image,
)
from ocr_pipeline.stages.sweep import sweep_page
from ocr_pipeline.stages.features import (
    build_style_signature, aggregate_signatures,
)
from ocr_pipeline.stages.store import (
    identify_low_confidence_regions, compute_batch_summary,
    calibrate_preproc_params,
)
from ocr_pipeline.stages.ocr_probe import probe_page, detect_tesseract_lang
from ocr_pipeline.types import ConfidenceRecord


def run_pass_a(config: PipelineConfig,
               issues: list,
               store: ArtifactStore,
               tess_lang: "str | None",
               save_debug: bool = False) -> dict:
    """
    Run Pass A: fast sweep across all issues.

    For each page of each issue:
      1. Ingest image + metadata
      2. Sweep: flatten illumination, threshold, extract components
      3. Extract features → build style signature
      4. OCR probe on strong regions → confidence records
      5. Identify low-confidence regions → Pass B targets

    Args:
        config: Pipeline configuration
        issues: List of issue dicts from all_issues.json
        store: ArtifactStore for persisting results
        tess_lang: Tesseract language string (or None)
        save_debug: Whether to save debug images

    Returns:
        Summary dict with aggregate statistics.
    """
    images_dir = Path(config.collection_dir) / "images"
    all_signatures = []
    total_pages = 0
    total_words = 0
    total_hc_words = 0
    pages_processed = 0
    pages_skipped = 0

    pipeline_log(f"Pass A: {len(issues)} issues, "
                 f"Tesseract={tess_lang or 'unavailable'}")

    for issue_idx, issue in enumerate(issues):
        ark_id = issue["ark_id"]
        n_pages = int(issue.get("pages", 8))
        issue_date = issue.get("date", "")

        pipeline_log(f"[{issue_idx+1}/{len(issues)}] {ark_id} "
                     f"({issue_date}, {n_pages}pp)")

        page_signatures = []

        for pg in range(1, n_pages + 1):
            total_pages += 1
            img_path = image_path(images_dir, ark_id, pg)

            if not is_valid_image(img_path):
                pages_skipped += 1
                pipeline_log(f"  p{pg:02d} SKIP (no image)", level="warn")
                continue

            # Phase 2: Ingest
            meta, img_gray = ingest_page(images_dir, issue, pg)
            if img_gray is None:
                pages_skipped += 1
                continue

            # Phase 3: Sweep
            with StageTimer("sweep", Path(config.artifact_dir),
                            ark_id=ark_id, page=pg):
                sweep_result = sweep_page(img_gray)

            flattened = sweep_result["flattened"]
            binary = sweep_result["binary"]
            components = sweep_result["components"]
            conf_map = sweep_result["conf_map"]
            stats = sweep_result["stats"]

            pipeline_log(f"  p{pg:02d} sweep: {stats['n_components']} comps, "
                         f"conf={stats['mean_confidence']:.2f}, "
                         f"strong={stats['strong_cells']}/weak={stats['weak_cells']}")

            # Phase 4: Features → style signature
            sig = build_style_signature(
                ark_id, issue_date, img_gray, binary, components, conf_map)
            page_signatures.append(sig)

            # Phase 6: OCR probe on strong regions
            if tess_lang:
                with StageTimer("ocr_probe", Path(config.artifact_dir),
                                ark_id=ark_id, page=pg):
                    records, probe_stats = probe_page(
                        flattened, conf_map, ark_id, pg, tess_lang)

                if records:
                    # Phase 5: Store confidence records
                    store.save_page_confidence(ark_id, pg, records)
                    total_words += probe_stats["total_words"]
                    total_hc_words += probe_stats["high_conf_words"]

                    # Identify low-confidence regions
                    lc_regions = identify_low_confidence_regions(records)
                    if lc_regions:
                        store.save_low_conf_regions(ark_id, pg, lc_regions)

                    pipeline_log(
                        f"  p{pg:02d} probe: {probe_stats['total_words']} words, "
                        f"HC={probe_stats['high_conf_words']}, "
                        f"LC regions={len(lc_regions)}")
            else:
                pipeline_log(f"  p{pg:02d} probe: SKIP (no Tesseract)")

            # Debug images
            if save_debug:
                prefix = f"{ark_id}_p{pg:02d}"
                store.save_debug_image(flattened, f"{prefix}_flattened.png")
                store.save_debug_image(binary, f"{prefix}_binary.png")

            pages_processed += 1

        # Aggregate page signatures into issue-level signature
        if page_signatures:
            issue_sig = aggregate_signatures(page_signatures)
            all_signatures.append(issue_sig)

    # Persist style signatures
    store.save_style_signatures(all_signatures)

    # Compute and save batch summary
    summary = compute_batch_summary(store, config.title_name, issues)
    store.save_batch_summary(summary)

    result = {
        "issues": len(issues),
        "pages_total": total_pages,
        "pages_processed": pages_processed,
        "pages_skipped": pages_skipped,
        "total_words": total_words,
        "high_conf_words": total_hc_words,
        "high_conf_fraction": round(total_hc_words / max(total_words, 1), 3),
        "signatures": len(all_signatures),
    }

    pipeline_log(f"\nPass A complete:")
    pipeline_log(f"  Pages: {pages_processed}/{total_pages} "
                 f"(skipped {pages_skipped})")
    pipeline_log(f"  Words: {total_words} total, "
                 f"{total_hc_words} high-confidence "
                 f"({result['high_conf_fraction']:.1%})")
    pipeline_log(f"  Signatures: {len(all_signatures)} issues")
    pipeline_log(f"  Artifacts: {config.artifact_dir}")

    return result


def main():
    p = argparse.ArgumentParser(
        description="Batch-aware OCR preprocessing pipeline — Pass A")
    p.add_argument("--config-path", required=True,
                   help="Path to collection.json")
    p.add_argument("--ark", default=None,
                   help="Process single issue by ARK ID")
    p.add_argument("--date-from", default=None,
                   help="Filter: earliest issue date (YYYY-MM-DD)")
    p.add_argument("--date-to", default=None,
                   help="Filter: latest issue date (YYYY-MM-DD)")
    p.add_argument("--save-debug-images", action="store_true",
                   help="Save intermediate images to artifacts/debug/")
    args = p.parse_args()

    config_path = Path(args.config_path)
    if not config_path.exists():
        sys.exit(f"Config not found: {config_path}")

    config = PipelineConfig.from_collection_json(config_path)
    config.save_debug_images = args.save_debug_images

    # Load issue index
    metadata_dir = config_path.parent / "metadata"
    issues = load_issue_index(metadata_dir)
    if not issues:
        sys.exit(f"No issues found in {metadata_dir}/all_issues.json")

    # Apply filters
    if args.ark:
        issues = [i for i in issues if i["ark_id"] == args.ark]
    if args.date_from:
        issues = [i for i in issues if i.get("date", "") >= args.date_from]
    if args.date_to:
        issues = [i for i in issues if i.get("date", "") <= args.date_to]

    if not issues:
        sys.exit("No issues match the specified filters.")

    pipeline_log(f"Collection: {config.title_name}")
    pipeline_log(f"Issues: {len(issues)}")
    pipeline_log(f"Layout: {config.layout_type}, {config.typeface}")

    # Initialize
    store = ArtifactStore(Path(config.collection_dir))
    store.init()

    tess_lang = detect_tesseract_lang()

    # Run Pass A
    result = run_pass_a(config, issues, store, tess_lang,
                        save_debug=config.save_debug_images)

    # Print calibrated parameters for the first issue (example)
    sigs = store.load_style_signatures()
    if sigs:
        from ocr_pipeline.types import StyleSignature
        first_sig = StyleSignature.from_dict(sigs[0])
        params = calibrate_preproc_params(first_sig)
        pipeline_log(f"\nCalibrated params for {first_sig.ark_id}:")
        pipeline_log(f"  CLAHE clip={params.clahe_clip_limit}, "
                     f"tile={params.clahe_tile_size}")
        pipeline_log(f"  Border threshold={params.border_threshold}")
        pipeline_log(f"  Binary threshold={params.binary_threshold}")

    return result


if __name__ == "__main__":
    main()
