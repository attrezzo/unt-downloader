#!/usr/bin/env python3
"""
render_pdf.py — Renders a bilingual PDF from corrected German + English translation.

Produces a two-column layout: German (left) / English (right), with
issue metadata header and page numbers.
"""

import logging
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)

log = logging.getLogger(__name__)

FONT_GERMAN = "Helvetica"
FONT_ENGLISH = "Helvetica"
FONT_SIZE = 9
LEADING = 13


def render_pdf(
    german_path: Path,
    english_path: Path,
    output_path: Path,
    issue_id: str,
) -> None:
    """
    Render a bilingual PDF with German and English side-by-side.

    Args:
        german_path: Path to corrected German text
        english_path: Path to English translation
        output_path: Path for rendered PDF output
        issue_id: Issue identifier used in the header
    """
    german = german_path.read_text(encoding="utf-8")
    english = english_path.read_text(encoding="utf-8")

    german_paras = [p.strip() for p in german.split("\n\n") if p.strip()]
    english_paras = [p.strip() for p in english.split("\n\n") if p.strip()]

    # Pad to same length
    max_len = max(len(german_paras), len(english_paras))
    german_paras += [""] * (max_len - len(german_paras))
    english_paras += [""] * (max_len - len(english_paras))

    styles = getSampleStyleSheet()
    de_style = ParagraphStyle(
        "German", fontName=FONT_GERMAN, fontSize=FONT_SIZE, leading=LEADING
    )
    en_style = ParagraphStyle(
        "English", fontName=FONT_ENGLISH, fontSize=FONT_SIZE, leading=LEADING,
        textColor=colors.HexColor("#333333")
    )
    header_style = ParagraphStyle(
        "Header", fontName="Helvetica-Bold", fontSize=11, leading=14,
        spaceAfter=6
    )

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=1 * inch,
        bottomMargin=0.75 * inch,
    )

    story = []
    story.append(Paragraph(f"Bellville Wochenblatt — {issue_id}", header_style))
    story.append(Paragraph("German (corrected) | English (translation)", styles["Italic"]))
    story.append(Spacer(1, 0.2 * inch))

    col_width = (letter[0] - 1.5 * inch) / 2 - 0.1 * inch

    for de_text, en_text in zip(german_paras, english_paras):
        row = [[
            Paragraph(de_text, de_style),
            Paragraph(en_text, en_style),
        ]]
        t = Table(row, colWidths=[col_width, col_width])
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("LINEAFTER", (0, 0), (0, -1), 0.5, colors.lightgrey),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.1 * inch))

    doc.build(story)
    log.info(f"PDF rendered: {output_path}")
