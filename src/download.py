#!/usr/bin/env python3
"""
download.py — Fetches raw OCR text for a Bellville Wochenblatt issue from UNT Portal.

Extracts plain text from the OCR layer (NOT the HTML rendering) to keep
downstream translation token costs low.
"""

import logging
import os
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = os.getenv("UNT_BASE_URL", "https://texashistory.unt.edu")


def download_issue(issue_id: str, output_path: Path) -> None:
    """
    Download OCR text for a given issue and write to output_path.

    Args:
        issue_id: UNT portal issue identifier (e.g. 'meta-pth-12345')
        output_path: Where to write the raw OCR text file
    """
    url = f"{BASE_URL}/ark:/{issue_id}/ocr/"
    log.info(f"Fetching: {url}")

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    # Extract plain text from OCR page — avoid storing HTML
    soup = BeautifulSoup(resp.text, "lxml")
    ocr_div = soup.find("div", class_="ocr-text") or soup.find("pre")
    if ocr_div:
        text = ocr_div.get_text(separator="\n")
    else:
        # Fallback: strip all tags
        text = soup.get_text(separator="\n")
        log.warning("Could not find dedicated OCR element; falling back to full page text strip")

    output_path.write_text(text.strip(), encoding="utf-8")
    log.info(f"Wrote {len(text)} chars to {output_path}")

    # Be polite to UNT servers
    time.sleep(1)
