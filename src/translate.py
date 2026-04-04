#!/usr/bin/env python3
"""
translate.py — Translates corrected German text to English via Claude.

COST NOTE: This is the most expensive pipeline stage. Text is sent as
plain extracted OCR (not HTML) to minimize token usage. Tune
TRANSLATE_CHUNK_SIZE in config to balance cost vs. context coherence.
"""

import logging
import os
import time
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

CHUNK_SIZE = int(os.getenv("TRANSLATE_CHUNK_SIZE", 2000))
DELAY_MS = int(os.getenv("TRANSLATE_DELAY_MS", 500))

SYSTEM_PROMPT = """You are a historian and expert translator of 19th-century German.
Translate the provided German newspaper text (Bellville Wochenblatt, 1870s Texas) to English.
- Produce fluent, readable English that preserves the original meaning and register
- Retain proper nouns, place names, and personal names as-is
- Do NOT add commentary, notes, or explanations — output the translation only
- Preserve paragraph structure"""


def translate_text(input_path: Path, output_path: Path) -> None:
    """
    Translate a corrected German text file to English.

    Args:
        input_path: Path to OCR-corrected German text
        output_path: Path to write English translation
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    model = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-5")

    text = input_path.read_text(encoding="utf-8")
    chunks = _chunk_text(text, CHUNK_SIZE)
    translated_chunks = []

    for i, chunk in enumerate(chunks, 1):
        log.info(f"Translating chunk {i}/{len(chunks)} ({len(chunk)} chars)")
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": chunk}],
        )
        translated_chunks.append(response.content[0].text)
        time.sleep(DELAY_MS / 1000)

    output_path.write_text("\n\n".join(translated_chunks), encoding="utf-8")
    log.info(f"Translation complete: {output_path}")


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
