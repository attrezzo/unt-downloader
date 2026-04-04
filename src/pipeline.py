#!/usr/bin/env python3
"""
pipeline.py — Orchestrates the full unt-downloader workflow.
Runs download → ocr_correct → translate → render_pdf for a given issue or date range.
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

from download import download_issue
from ocr_correct import correct_ocr
from translate import translate_text
from render_pdf import render_pdf

load_dotenv("config/secrets.env")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(os.getenv("LOG_DIR", "logs")) / "pipeline.log"),
    ],
)
log = logging.getLogger(__name__)


def run_pipeline(issue_id: str, skip_ocr_correct: bool = False) -> Path:
    out_dir = Path(os.getenv("OUTPUT_DIR", "output")) / issue_id
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"=== Starting pipeline for issue: {issue_id} ===")

    # Stage 1: Download
    raw_path = out_dir / "raw_ocr.txt"
    if not raw_path.exists():
        log.info("Stage 1: Downloading...")
        download_issue(issue_id, raw_path)
    else:
        log.info("Stage 1: Skipped (raw_ocr.txt exists)")

    # Stage 2: OCR Correction
    corrected_path = out_dir / "corrected.txt"
    if not corrected_path.exists():
        if skip_ocr_correct:
            log.info("Stage 2: Skipped (SKIP_OCR_CORRECT=1)")
            corrected_path = raw_path
        else:
            log.info("Stage 2: Correcting OCR...")
            correct_ocr(raw_path, corrected_path)
    else:
        log.info("Stage 2: Skipped (corrected.txt exists)")

    # Stage 3: Translate
    translated_path = out_dir / "translated.txt"
    if not translated_path.exists():
        log.info("Stage 3: Translating...")
        translate_text(corrected_path, translated_path)
    else:
        log.info("Stage 3: Skipped (translated.txt exists)")

    # Stage 4: Render PDF
    pdf_path = out_dir / f"{issue_id}.pdf"
    if not pdf_path.exists():
        log.info("Stage 4: Rendering PDF...")
        render_pdf(corrected_path, translated_path, pdf_path, issue_id)
    else:
        log.info("Stage 4: Skipped (PDF exists)")

    log.info(f"=== Done: {pdf_path} ===")
    return pdf_path


def main():
    parser = argparse.ArgumentParser(description="UNT Bellville Wochenblatt pipeline")
    parser.add_argument("--issue", help="Single issue ID to process")
    parser.add_argument("--start", help="Start date YYYY-MM-DD (for date range runs)")
    parser.add_argument("--end", help="End date YYYY-MM-DD (for date range runs)")
    parser.add_argument("--skip-ocr-correct", action="store_true",
                        help="Skip OCR correction stage")
    args = parser.parse_args()

    skip = args.skip_ocr_correct or os.getenv("SKIP_OCR_CORRECT", "0") == "1"

    if args.issue:
        run_pipeline(args.issue, skip_ocr_correct=skip)
    elif args.start and args.end:
        # Placeholder: enumerate issues by date range
        # TODO: implement UNT collection date enumeration
        log.error("Date range enumeration not yet implemented. Use --issue for now.")
        sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
