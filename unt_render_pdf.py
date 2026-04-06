#!/usr/bin/env python3
"""
unt_render_pdf.py — Render translated UNT newspaper issues as formatted PDFs

Reads translated .txt files and produces newspaper-style PDFs using ReportLab.
No Claude API calls — structure is inferred from text patterns only.

For pages where translation failed (raw HTML present), the original page
image is embedded as a fallback.

USAGE (via downloader):
  python unt_archive_downloader.py --render-pdf [--resume] [--columns 5]

STANDALONE:
  python unt_render_pdf.py \\
    --translated-dir /path/to/translated \\
    --images-dir /path/to/images \\
    --out-dir /path/to/pdf

  python unt_render_pdf.py \\
    --config-path bellville_wochenblatt/collection.json \\
    --ark metapth1478562
"""

import re, sys, os, json, argparse
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate,
    Paragraph, Spacer, KeepTogether, Image as RLImage
)
from reportlab.platypus.flowables import Flowable

# ---------------------------------------------------------------------------
# Global paths (set when running via downloader)
# ---------------------------------------------------------------------------
TRANSLATED_DIR = None
IMAGES_DIR     = None
PDF_DIR        = None

def init_paths(collection_dir: Path):
    global TRANSLATED_DIR, IMAGES_DIR, PDF_DIR
    TRANSLATED_DIR = collection_dir / "output" / "translated"
    IMAGES_DIR     = collection_dir / "sources" / "images"
    PDF_DIR        = collection_dir / "output" / "pdf"


# ---------------------------------------------------------------------------
# SAFE TEXT HANDLING
#
# Core principle: source text is ALWAYS stripped to plain text before
# reaching ReportLab. We then re-add only markup WE control (<b>, <i>).
# This means OCR garbage, mismatched HTML tags, and entity soup can never
# crash ReportLab's XML parser.
# ---------------------------------------------------------------------------

def to_plain(text: str) -> str:
    """Strip all HTML/markdown to pure plain text."""
    text = re.sub(r'<[^>]{0,300}>', ' ', text)       # strip HTML tags
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '').replace('&gt;', '')
    text = text.replace('&quot;', '"').replace('&apos;', "'")
    text = text.replace('&#x27;', "'").replace('&nbsp;', ' ')
    text = re.sub(r'&#[xX][0-9a-fA-F]{1,6};', '', text)
    text = re.sub(r'&#\d{1,6};', '', text)
    text = re.sub(r'\*{1,3}', '', text)               # markdown bold/italic
    text = re.sub(r'_{1,2}([^_]+)_{1,2}', r'\1', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def safe_rl(text: str) -> str:
    """
    Produce ReportLab-safe markup from source text.
    Preserves **bold** → <b>bold</b>. Everything else is plain + XML-escaped.
    """
    # Capture **bold** spans before stripping
    bold_re = re.compile(r'\*\*(.{1,150}?)\*\*')
    saved = {}
    idx = [0]

    def capture(m):
        key = f'\x01{idx[0]}\x01'
        saved[key] = to_plain(m.group(1))
        idx[0] += 1
        return key

    text = bold_re.sub(capture, text)
    text = to_plain(text)

    # XML-escape
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    # Restore bold
    for key, val in saved.items():
        safe_val = val.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        # The key itself may have been mangled by to_plain; use raw key chars
        text = text.replace(key, f'<b>{safe_val}</b>')

    # Clean any leftover placeholder artifacts
    text = re.sub(r'\x01\d+\x01', '', text)
    return text.strip()


def make_para(text: str, style) -> Paragraph:
    """Create a Paragraph with automatic fallback on any markup error."""
    markup = safe_rl(text)
    try:
        return Paragraph(markup, style)
    except Exception:
        # Total fallback: raw XML-escaped plain text, no bold
        plain = to_plain(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        try:
            return Paragraph(plain or '&nbsp;', style)
        except Exception:
            return Paragraph('&nbsp;', style)


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
SERIF      = "Times-Roman"
SERIF_BOLD = "Times-Bold"
SERIF_ITAL = "Times-Italic"
SERIF_BI   = "Times-BoldItalic"


# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
def make_styles():
    return {
        "headline1": ParagraphStyle("headline1",
            fontName=SERIF_BOLD, fontSize=13, leading=16,
            alignment=TA_CENTER, spaceBefore=5, spaceAfter=3),
        "headline2": ParagraphStyle("headline2",
            fontName=SERIF_BOLD, fontSize=10, leading=13,
            alignment=TA_CENTER, spaceBefore=4, spaceAfter=2),
        "headline3": ParagraphStyle("headline3",
            fontName=SERIF_BI, fontSize=9, leading=11,
            alignment=TA_CENTER, spaceBefore=3, spaceAfter=1),
        "dateline": ParagraphStyle("dateline",
            fontName=SERIF_BOLD, fontSize=7.5, leading=9,
            alignment=TA_LEFT, spaceAfter=1),
        "body_first": ParagraphStyle("body_first",
            fontName=SERIF, fontSize=8, leading=10.5,
            alignment=TA_JUSTIFY, firstLineIndent=0, spaceAfter=3),
        "body": ParagraphStyle("body",
            fontName=SERIF, fontSize=8, leading=10.5,
            alignment=TA_JUSTIFY, firstLineIndent=10, spaceAfter=3),
        "body_italic": ParagraphStyle("body_italic",
            fontName=SERIF_ITAL, fontSize=8, leading=10.5,
            alignment=TA_JUSTIFY, firstLineIndent=10, spaceAfter=3),
        "ad_name": ParagraphStyle("ad_name",
            fontName=SERIF_BOLD, fontSize=9, leading=11,
            alignment=TA_CENTER, spaceBefore=4, spaceAfter=1),
        "ad_body": ParagraphStyle("ad_body",
            fontName=SERIF, fontSize=7.5, leading=9.5,
            alignment=TA_CENTER, spaceAfter=1),
        "attribution": ParagraphStyle("attribution",
            fontName=SERIF_ITAL, fontSize=7, leading=9,
            alignment=TA_RIGHT, spaceAfter=3,
            textColor=colors.Color(0.45, 0.45, 0.45)),
        "failed_note": ParagraphStyle("failed_note",
            fontName=SERIF_ITAL, fontSize=8, leading=10,
            alignment=TA_CENTER,
            textColor=colors.Color(0.5, 0.5, 0.5)),
        "page_label": ParagraphStyle("page_label",
            fontName=SERIF_ITAL, fontSize=6.5, leading=8,
            alignment=TA_RIGHT,
            textColor=colors.Color(0.55, 0.55, 0.55)),
    }


# ---------------------------------------------------------------------------
# Flowables
# ---------------------------------------------------------------------------
class ThinRule(Flowable):
    def __init__(self, width, thickness=0.5, color=colors.black, vpad=2):
        super().__init__()
        self.rule_width = width
        self.thickness  = thickness
        self.rule_color = color
        self.height     = thickness + vpad * 2
        self.vpad       = vpad

    def draw(self):
        self.canv.setStrokeColor(self.rule_color)
        self.canv.setLineWidth(self.thickness)
        self.canv.line(0, self.vpad, self.rule_width, self.vpad)


class NextPageTemplate(Flowable):
    def __init__(self, name):
        super().__init__()
        self.name = name
        self.width = self.height = 0

    def draw(self):
        self.canv._doctemplate.handle_nextPageTemplate(self.name)


# ---------------------------------------------------------------------------
# Line classifier
# ---------------------------------------------------------------------------
PAGE_MARKER_RE = re.compile(r'^---\s*Page\s+(\d+)\s+of\s+(\d+)\s*---\s*$')
SECTION_SEP_RE = re.compile(r'^-{2,}\s*$')
SKIP_RE        = re.compile(r'^\[(TRANSLATED TO ENGLISH|Corrected OCR used)[^\]]*\]', re.I)
FAIL_RE        = re.compile(r'^\[TRANSLATION MISSING', re.I)
SOURCE_OCR_RE  = re.compile(r'^\[Source OCR\]', re.I)
ATTRIBUTION_RE = re.compile(r'^\s*\([-\w\s\./,]+\)\s*$')
VERSE_RE       = re.compile(r'^\[verse\]', re.I)
DATELINE_RE    = re.compile(
    r'^([A-Z][A-Za-z\s\.\,\-]{2,35}),\s+'
    r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\.?',
    re.I
)


def caps_frac(s):
    alpha = [c for c in s if c.isalpha()]
    return sum(1 for c in alpha if c.isupper()) / len(alpha) if alpha else 0


def classify(line: str) -> str:
    s = line.strip()
    if not s:                             return 'blank'
    if PAGE_MARKER_RE.match(s):           return 'page_marker'
    if SKIP_RE.match(s):                  return 'skip'
    if FAIL_RE.match(s):                  return 'fail'
    if SOURCE_OCR_RE.match(s):            return 'fail'
    if SECTION_SEP_RE.match(s):           return 'sep'
    if VERSE_RE.match(s):                 return 'verse'
    if ATTRIBUTION_RE.match(s) and len(s) < 60:
                                          return 'attr'
    if DATELINE_RE.match(s):              return 'dateline'

    plain = re.sub(r'\*+', '', s)
    cf = caps_frac(plain)
    words = re.findall(r'[A-Za-z]{3,}', plain)
    if cf > 0.82 and words:
        if len(plain) > 50:               return 'h1'
        if len(plain) > 20:               return 'h2'
        return 'h3'

    if re.match(r'^\*\*.{3,80}\*\*\.?$', s) and len(s) < 90:
        return 'ad_name'

    return 'body'


# ---------------------------------------------------------------------------
# Parse translated file
# ---------------------------------------------------------------------------
def parse_file(path: Path) -> dict:
    raw = path.read_bytes().decode('utf-8', errors='replace')
    raw = raw.replace('\r\n', '\n').replace('\r', '\n')
    lines = raw.splitlines()

    header = {}
    body_start = 0
    for i, line in enumerate(lines):
        s = line.strip()
        if s.startswith('=== ') and s.endswith(' ==='):
            header['collection'] = s[4:-4].strip()
        elif s.startswith('ARK:'):
            header['ark'] = s[4:].strip()
        elif s.startswith('Date:'):
            header['date'] = s[5:].strip()
        elif s.startswith('Volume:'):
            m = re.match(r'Volume:\s*(\S+)\s+Number:\s*(\S+)', s)
            if m:
                header['volume'] = m.group(1)
                header['number'] = m.group(2)
        elif s.startswith('Title:'):
            header['title'] = s[6:].strip()
        elif s.startswith('=' * 10):
            body_start = i + 1
            break

    pages = {}
    cur_page  = None
    cur_lines = []

    for line in lines[body_start:]:
        m = PAGE_MARKER_RE.match(line.strip())
        if m:
            if cur_page is not None:
                pages[cur_page] = _process_page(cur_lines)
            cur_page  = int(m.group(1))
            cur_lines = []
        elif cur_page is not None:
            cur_lines.append(line)

    if cur_page is not None:
        pages[cur_page] = _process_page(cur_lines)

    return {'header': header, 'pages': pages}


def _ocr_from_html(text: str) -> str:
    """Extract plain text from raw HTML OCR content. Returns clean plain text."""
    m = re.search(r'id=["\']ocr-text["\'][^>]*>(.*?)</(?:div|section)',
                  text, re.S | re.I)
    inner = m.group(1) if m else text
    inner = re.sub(r'<br\s*/?>', '\n', inner, flags=re.I)
    inner = re.sub(r'<[^>]{0,500}>', ' ', inner)
    inner = inner.replace('&amp;', '&').replace('&lt;', '').replace('&gt;', '')
    inner = inner.replace('&quot;', '"').replace('&#x27;', "'")
    inner = re.sub(r'&#[xX][0-9a-fA-F]+;', '', inner)
    inner = re.sub(r'&#\d+;', '', inner)
    inner = re.sub(r'[ \t]{2,}', ' ', inner)
    inner = re.sub(r'\n{3,}', '\n\n', inner)
    return inner.strip()


def _process_page(lines: list) -> list:
    """Return list of (tag, content) blocks for one page."""
    while lines and not lines[0].strip():
        lines = lines[1:]
    while lines and not lines[-1].strip():
        lines = lines[:-1]

    if not lines:
        return [('image_only', '')]

    full = '\n'.join(lines)

    # Detect failure: TRANSLATION MISSING marker or raw HTML
    if (FAIL_RE.search(full)
            or re.search(r'<!DOCTYPE|<html\b|<head>', full[:500], re.I)):
        return [('failed', _ocr_from_html(full))]

    blocks     = []
    i          = 0
    n          = len(lines)
    in_verse   = False
    after_head = True

    while i < n:
        tag = classify(lines[i])
        s   = lines[i].strip()

        if tag in ('blank', 'skip', 'page_marker', 'fail'):
            if tag == 'blank':
                after_head = True
            i += 1
            continue

        if tag == 'verse':
            in_verse = True
            i += 1
            continue

        if tag == 'sep':
            blocks.append(('sep', ''))
            in_verse = False
            after_head = True
            i += 1
            continue

        if tag == 'attr':
            blocks.append(('attr', s))
            i += 1
            continue

        if tag in ('h1', 'h2', 'h3'):
            parts = [s]
            i += 1
            while i < n and classify(lines[i]) in ('h1', 'h2', 'h3') and lines[i].strip():
                parts.append(lines[i].strip())
                i += 1
            blocks.append((tag, '\n'.join(parts)))
            after_head = True
            in_verse   = False
            continue

        if tag == 'ad_name':
            ad = [s]
            i += 1
            while i < n:
                nt = classify(lines[i])
                ns = lines[i].strip()
                if nt in ('sep', 'h1', 'h2', 'h3', 'ad_name') or not ns:
                    break
                ad.append(ns)
                i += 1
            blocks.append(('ad', ad))
            after_head = True
            continue

        if tag == 'dateline':
            blocks.append(('dateline', s))
            after_head = True
            i += 1
            continue

        if tag == 'body':
            para = [s]
            i += 1
            while i < n and classify(lines[i]) == 'body' and lines[i].strip():
                para.append(lines[i].strip())
                i += 1
            if in_verse:
                btag = 'body_italic'
            elif after_head:
                btag = 'body_first'
            else:
                btag = 'body'
            blocks.append((btag, ' '.join(para)))
            after_head = False
            continue

        i += 1

    return blocks


# ---------------------------------------------------------------------------
# Build flowables
# ---------------------------------------------------------------------------
def to_flowables(blocks: list, styles: dict, col_w: float) -> list:
    out = []
    grey = colors.Color(0.5, 0.5, 0.5)
    dark = colors.Color(0.3, 0.3, 0.3)

    for tag, content in blocks:

        if tag == 'sep':
            out += [Spacer(1, 3),
                    ThinRule(col_w, 0.5, grey, 1),
                    Spacer(1, 3)]

        elif tag == 'h1':
            out.append(Spacer(1, 4))
            out.append(ThinRule(col_w, 1.2))
            out.append(Spacer(1, 2))
            for part in content.split('\n'):
                if part.strip():
                    out.append(make_para(part, styles['headline1']))
            out.append(ThinRule(col_w, 0.5))
            out.append(Spacer(1, 2))

        elif tag == 'h2':
            out.append(Spacer(1, 4))
            for part in content.split('\n'):
                if part.strip():
                    out.append(make_para(part, styles['headline2']))
            out.append(ThinRule(col_w, 0.5, dark, 1))
            out.append(Spacer(1, 2))

        elif tag == 'h3':
            out.append(Spacer(1, 3))
            for part in content.split('\n'):
                if part.strip():
                    out.append(make_para(part, styles['headline3']))
            out.append(Spacer(1, 1))

        elif tag == 'dateline':
            out.append(make_para(content, styles['dateline']))

        elif tag in ('body', 'body_first', 'body_italic'):
            out.append(make_para(content, styles[tag]))

        elif tag == 'attr':
            out.append(make_para(content, styles['attribution']))

        elif tag == 'ad':
            inner = []
            first = True
            for line in content:
                st = styles['ad_name'] if first else styles['ad_body']
                inner.append(make_para(line, st))
                first = False
            out += [Spacer(1, 5), KeepTogether(inner), Spacer(1, 5)]

    return out


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------
def find_image(ark_id: str, page: int, images_dir: Path):
    if not images_dir:
        return None
    for pat in [f'page_{page:02d}.jpg', f'page_{page}.jpg']:
        p = images_dir / ark_id / pat
        if p.exists() and p.stat().st_size > 5000:
            return p
    return None


def fit_image(img_path: Path, max_w: float, max_h: float) -> RLImage:
    from PIL import Image as PILImage
    with PILImage.open(img_path) as im:
        iw, ih = im.size
    aspect = ih / iw
    w = min(max_w, max_h / aspect)
    h = w * aspect
    if h > max_h:
        h = max_h
        w = h / aspect
    return RLImage(str(img_path), width=w, height=h)


# ---------------------------------------------------------------------------
# Page decorators
# ---------------------------------------------------------------------------
def make_decorators(header, collection_name, margin, masthead_bot_y):
    title = re.sub(r'\. \(.*?\),?.*', '', header.get('title', collection_name)).strip()
    date  = header.get('date', '')
    vol   = header.get('volume', '?')
    num   = header.get('number', '?')
    pw, ph = letter

    def _footer(canvas):
        canvas.saveState()
        canvas.setStrokeColor(colors.black)
        canvas.setLineWidth(0.4)
        canvas.line(margin, 0.52 * inch, pw - margin, 0.52 * inch)
        canvas.setFont(SERIF, 6.5)
        canvas.setFillColor(colors.Color(0.35, 0.35, 0.35))
        canvas.drawString(margin, 0.36 * inch,
                          f"{title}  \xb7  Vol. {vol}, No. {num}")
        canvas.drawRightString(pw - margin, 0.36 * inch,
                               f"{date}  \xb7  p. {canvas.getPageNumber()}")
        canvas.restoreState()

    def _col_rules(canvas, top_y, doc):
        canvas.setStrokeColor(colors.Color(0.72, 0.72, 0.72))
        canvas.setLineWidth(0.3)
        for c in range(1, doc._col_count):
            x = margin + c * (doc._col_width + doc._col_gutter) - doc._col_gutter / 2
            canvas.line(x, 0.55 * inch, x, top_y)

    def first_page(canvas, doc):
        _footer(canvas)
        canvas.saveState()
        canvas.setStrokeColor(colors.black)
        canvas.setLineWidth(2.0)
        canvas.line(margin, ph - 0.46 * inch, pw - margin, ph - 0.46 * inch)
        canvas.setLineWidth(0.5)
        canvas.line(margin, ph - 0.46 * inch - 3, pw - margin, ph - 0.46 * inch - 3)
        canvas.setFont(SERIF_BOLD, 21)
        canvas.setFillColor(colors.black)
        canvas.drawCentredString(pw / 2, ph - 0.72 * inch, title)
        canvas.setFont(SERIF, 7.5)
        canvas.setFillColor(colors.Color(0.25, 0.25, 0.25))
        sub = (f"Vol. {vol}  \xb7  No. {num}  \xb7  {date}  \xb7  "
               f"Bellville, Austin County, Texas  \xb7  [English translation - Portal to Texas History]")
        canvas.drawCentredString(pw / 2, ph - 0.86 * inch, sub)
        canvas.setStrokeColor(colors.black)
        canvas.setLineWidth(0.8)
        canvas.line(margin, masthead_bot_y, pw - margin, masthead_bot_y)
        _col_rules(canvas, masthead_bot_y, doc)
        canvas.restoreState()

    def later_page(canvas, doc):
        _footer(canvas)
        canvas.saveState()
        canvas.setStrokeColor(colors.black)
        canvas.setLineWidth(0.8)
        canvas.line(margin, ph - 0.5 * inch, pw - margin, ph - 0.5 * inch)
        _col_rules(canvas, ph - 0.52 * inch, doc)
        canvas.restoreState()

    return first_page, later_page


# ---------------------------------------------------------------------------
# Render one issue
# ---------------------------------------------------------------------------
def render_issue_pdf(parsed: dict, out_path: Path,
                     collection_name: str,
                     images_dir: Path = None,
                     columns: int = 5) -> None:

    out_path.parent.mkdir(parents=True, exist_ok=True)

    pw, ph   = letter
    margin   = 0.50 * inch
    bot_mar  = 0.62 * inch
    gutter   = 0.12 * inch
    usable_w = pw - 2 * margin
    col_w    = (usable_w - (columns - 1) * gutter) / columns

    styles = make_styles()
    header = parsed['header']
    pages  = parsed['pages']
    ark    = header.get('ark', '')

    masthead_h     = 1.02 * inch
    top_p1         = masthead_h
    top_later      = 0.58 * inch
    masthead_bot_y = ph - top_p1

    def make_frames(top_mar):
        return [
            Frame(margin + c * (col_w + gutter), bot_mar,
                  col_w, ph - top_mar - bot_mar,
                  leftPadding=0, rightPadding=0,
                  topPadding=0, bottomPadding=0,
                  showBoundary=0, id=f'col{c}')
            for c in range(columns)
        ]

    first_dec, later_dec = make_decorators(header, collection_name,
                                            margin, masthead_bot_y)

    doc = BaseDocTemplate(str(out_path), pagesize=letter,
                          leftMargin=margin, rightMargin=margin,
                          topMargin=top_p1, bottomMargin=bot_mar)
    doc._col_count  = columns
    doc._col_gutter = gutter
    doc._col_width  = col_w

    doc.addPageTemplates([
        PageTemplate('First', frames=make_frames(top_p1),    onPage=first_dec),
        PageTemplate('Later', frames=make_frames(top_later), onPage=later_dec),
    ])

    story = [NextPageTemplate('First'), NextPageTemplate('Later')]

    sorted_pages = sorted(pages.keys())
    total        = max(pages.keys())

    for idx, pg_num in enumerate(sorted_pages):
        blocks = pages[pg_num]

        # Page divider (skip before first page)
        if idx > 0:
            story += [
                Spacer(1, 3),
                ThinRule(col_w, 1.2, colors.Color(0.2, 0.2, 0.2), 1),
                make_para(f'- Original page {pg_num} of {total} -',
                          styles['page_label']),
                Spacer(1, 4),
            ]

        # Failed page?
        if blocks and blocks[0][0] in ('failed', 'image_only'):
            ocr_text = blocks[0][1]
            img_path = find_image(ark, pg_num, images_dir)

            if img_path:
                story.append(make_para(
                    f'[Page {pg_num}: translation unavailable - original scan below]',
                    styles['failed_note']))
                story.append(Spacer(1, 4))
                avail_h = ph - top_p1 - bot_mar - 0.4 * inch
                story.append(fit_image(img_path, col_w, avail_h))
            elif ocr_text:
                story.append(make_para(
                    f'[Page {pg_num}: translation unavailable - raw OCR text below]',
                    styles['failed_note']))
                story.append(Spacer(1, 4))
                for chunk in ocr_text.split('\n'):
                    chunk = chunk.strip()
                    if chunk and len(chunk) > 4:
                        story.append(make_para(chunk, styles['body']))
            else:
                story.append(make_para(
                    f'[Page {pg_num}: no content available]',
                    styles['failed_note']))
        else:
            story.extend(to_flowables(blocks, styles, col_w))

    doc.build(story)


# ---------------------------------------------------------------------------
# Standalone mode
# ---------------------------------------------------------------------------
def render_standalone(translated_dir: Path, images_dir: Path,
                       out_dir: Path, columns: int, resume: bool,
                       ark_filter: str = None):
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_files = sorted(translated_dir.glob('*.txt'))
    if ark_filter:
        txt_files = [f for f in txt_files if ark_filter in f.name]
    if not txt_files:
        print(f'No .txt files in {translated_dir}')
        return

    print(f'Rendering {len(txt_files)} file(s) -> {out_dir}/')
    ok = skip = err = 0
    for txt in txt_files:
        pdf_path = out_dir / txt.with_suffix('.pdf').name
        if resume and pdf_path.exists() and pdf_path.stat().st_size > 5000:
            print(f'  SKIP  {txt.name}')
            skip += 1
            continue
        print(f'  {txt.name} ...', end='', flush=True)
        try:
            parsed = parse_file(txt)
            name   = parsed['header'].get('collection', 'Bellville Wochenblatt')
            render_issue_pdf(parsed, pdf_path, name,
                             images_dir=images_dir, columns=columns)
            kb = pdf_path.stat().st_size // 1024
            print(f'  OK  {kb} KB')
            ok += 1
        except Exception as e:
            import traceback
            print(f'  FAIL  {e}')
            traceback.print_exc()
            err += 1

    print(f'\nDone: {ok} rendered, {skip} skipped, {err} errors')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description='Render translated UNT newspaper issues as PDFs')
    p.add_argument('--config-path',    default=None)
    p.add_argument('--translated-dir', default=None)
    p.add_argument('--images-dir',     default=None)
    p.add_argument('--out-dir',        default=None)
    p.add_argument('--ark',            default=None)
    p.add_argument('--date-from',      default=None)
    p.add_argument('--date-to',        default=None)
    p.add_argument('--columns',        type=int, default=5)
    p.add_argument('--resume',         action='store_true')
    args = p.parse_args()

    if args.translated_dir:
        render_standalone(
            Path(args.translated_dir),
            Path(args.images_dir) if args.images_dir else None,
            Path(args.out_dir) if args.out_dir else Path(args.translated_dir).parent / 'pdf',
            args.columns, args.resume, args.ark)
        return

    if not args.config_path:
        p.error('Provide --config-path or --translated-dir')

    config_path = Path(args.config_path)
    with open(config_path, encoding='utf-8') as f:
        config = json.load(f)

    collection_dir  = config_path.parent
    collection_name = config['title_name']
    init_paths(collection_dir)

    imd     = Path(args.images_dir) if args.images_dir else IMAGES_DIR
    pdf_out = Path(args.out_dir) if args.out_dir else PDF_DIR
    pdf_out.mkdir(parents=True, exist_ok=True)

    index_path = collection_dir / 'metadata' / 'all_issues.json'
    with open(index_path, encoding='utf-8') as f:
        all_issues = json.load(f)

    issues = all_issues
    if args.ark:       issues = [i for i in issues if i['ark_id'] == args.ark]
    if args.date_from: issues = [i for i in issues if i.get('date','') >= args.date_from]
    if args.date_to:   issues = [i for i in issues if i.get('date','') <= args.date_to]

    ok = skip = err = 0
    for i, issue in enumerate(issues):
        ark_id = issue['ark_id']
        vol    = str(issue.get('volume', '?')).zfill(2)
        num    = str(issue.get('number', '?')).zfill(2)
        date   = re.sub(r'[^\w\-]', '-', issue.get('date', 'unknown'))
        fname  = f'{ark_id}_vol{vol}_no{num}_{date}'

        trans_path = TRANSLATED_DIR / f'{fname}.txt'
        pdf_path   = pdf_out / f'{fname}.pdf'
        print(f'[{i+1:02d}/{len(issues)}] {ark_id} ...', end='', flush=True)

        if args.resume and pdf_path.exists() and pdf_path.stat().st_size > 5000:
            print(' SKIP'); skip += 1; continue
        if not trans_path.exists():
            print(' NO FILE'); err += 1; continue

        try:
            parsed = parse_file(trans_path)
            render_issue_pdf(parsed, pdf_path, collection_name,
                             images_dir=imd, columns=args.columns)
            print(f'  OK  {pdf_path.stat().st_size // 1024} KB')
            ok += 1
        except Exception as e:
            import traceback
            print(f'  FAIL  {e}')
            traceback.print_exc()
            err += 1

    print(f'\nComplete: {ok} rendered, {skip} skipped, {err} errors')


if __name__ == '__main__':
    main()
