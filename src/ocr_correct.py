#!/usr/bin/env python3
"""
ocr_correct.py — Uses Claude to correct OCR artifacts in raw German text.

Processes text in chunks to stay within API limits. Preserves paragraph
structure and handles common 19th-century German OCR failure modes
(long-s confusion, broken words, period/comma swaps, etc.).
"""

import logging
import os
import time
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

CHUNK_SIZE = int(os.getenv("TRANSLATE_CHUNK_SIZE", 2000))
DELAY_MS = int(os.getenv("TRANSLATE_DELAY_MS", 500))

SYSTEM_PROMPT = """You are an expert in 19th-century German typography and OCR correction.
Correct OCR errors in the provided German text from the Bellville Wochenblatt newspaper (1870s Texas).
- Fix broken words, misread characters (ſ→s, rn→m, etc.), and punctuation errors
- Preserve paragraph structure and line breaks
- Do NOT translate — output corrected German only
- Do NOT add commentary or explanation"""


def correct_ocr(input_path: Path, output_path: Path) -> None:
    """
    OCR-correct a file of raw German text using Claude.

    Args:
        input_path: Path to raw OCR text file
        output_path: Path to write corrected text
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    model = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")

    raw = input_path.read_text(encoding="utf-8")
    chunks = _chunk_text(raw, CHUNK_SIZE)
    corrected_chunks = []

    for i, chunk in enumerate(chunks, 1):
        log.info(f"OCR-correcting chunk {i}/{len(chunks)} ({len(chunk)} chars)")
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": chunk}],
        )
        corrected_chunks.append(response.content[0].text)
        time.sleep(DELAY_MS / 1000)

    output_path.write_text("\n\n".join(corrected_chunks), encoding="utf-8")
    log.info(f"OCR correction complete: {output_path}")


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    """Split text into chunks at paragraph boundaries."""
    paragraphs = text.split("\n\n")
    chunks = []
    current = []
    current_len = 0

    for para in paragraphs:
        if current_len + len(para) > chunk_size and current:
            chunks.append("\n\n".join(current))
            current = []
            current_len = 0
        current.append(para)
        current_len += len(para)

    if current:
        chunks.append("\n\n".join(current))

    return chunks
