---
name: fraktur-ocr
description: Multi-pass OCR correction pipeline for 19th-century German-language Fraktur newspapers scanned from microfilm. Use this skill whenever the user uploads a newspaper page image (JPG/PNG/TIFF) from a German-language Texas newspaper, mentions Fraktur OCR, references the Bellville Wochenblatt or similar German-Texan periodicals, asks to OCR old German text, or wants to correct/improve existing OCR of Fraktur script. Also trigger when the user mentions ABBYY OCR output from the Portal to Texas History (UNT), German-Texan historical newspapers, or any combination of "OCR" with "Fraktur," "German newspaper," "Wochenblatt," "Texas German," or "19th century German." This skill handles the full pipeline from raw image to corrected, metadata-tagged output.
---

# Fraktur OCR Pipeline

A three-pass pipeline for extracting readable text from 19th-century German-language Fraktur newspapers scanned from microfilm. Produces corrected text with machine-readable metadata tags that preserve provenance for future refinement.

## When to Use

- User uploads a newspaper page image and asks for OCR or transcription
- User provides existing ABBYY/traditional OCR output and asks for correction
- User wants to process German-Texan historical newspaper pages
- User mentions Fraktur, Wochenblatt, or German-language Texas newspapers
- User asks to compare or merge multiple OCR sources of the same page

## Prerequisites

- A page image (JPG/PNG/TIFF) of the newspaper page
- Optionally: existing OCR text (e.g., ABBYY output from Portal to Texas History)
- The user should specify the newspaper name, date, and page number if known

## Output Format

The pipeline produces **Markdown** with inline metadata tags. See `references/markup-spec.md` for the full tag specification.

**Key principle:** Every character in the output is either HIGH CONFIDENCE (unmarked) or tagged with metadata explaining its provenance and confidence level. Future AI passes can target only the tagged regions for refinement without re-processing the entire page.

---

## The Three Passes

### PASS 1 — Direct Fraktur OCR

Read the uploaded page image directly. Work section by section (masthead, then columns left to right, top to bottom).

**Instructions:**
1. Identify the page layout: masthead, column count, any center-page features (advertisements, program announcements)
2. Read each section in Fraktur, writing the text in standard Latin characters
3. Where text is **confidently readable**, write it directly — no tags needed
4. Where text is **illegible or uncertain**, insert a gap marker with your best guess and an approximate bounding box:
   ```
   {{ gap | est=NN | imgbbox="x,y,w,h" [best guess] }}
   ```
   - `NN` = estimated character count
   - `imgbbox` = approximate pixel region in the source image (x,y = top-left, w,h = size). Be generous — overestimate to ensure the text is fully contained. This allows future refinement passes to crop just this region instead of resending the full page.
   - `[best guess]` = your prediction based on context. Always guess.
5. Mark any images, illustrations, or engravings on the page:
   ```
   {{ Img | bbox="x,y,w,h" | desc="brief description" }}
   ```
6. Wrap each discrete article/news item/notice in a numbered Column tag:
   ```
   {{ Column001 }}
   ## Headline
   **Dateline,** Date. Article body...
   {{ /Column }}
   ```
7. Wrap each advertisement in a numbered Ad tag:
   ```
   {{ Ad001 }}
   Business name. Products/services. Address.
   {{ /Ad }}
   ```
8. Number Column and Ad tags sequentially per page (001, 002, 003...)
9. Mark article headlines with `##` and subheads with `###`
10. Preserve any visible datelines (city, date) at the start of news items in **bold**

**Before reading**, consult `references/fraktur-errors.md` to prime yourself on systematic Fraktur OCR failure modes. Apply these corrections as you read — e.g., when you see what looks like "b" but context demands "d", use "d".

**Texas German awareness:** Consult `references/texas-german.md` before reading. Do NOT normalize Texas German vocabulary to standard Hochdeutsch. Preserve English loanwords, hybrid compounds, period spellings, and Germanized place names exactly as printed.

**Output this pass as:**
```
## PASS 1 — Direct OCR
### Metadata
- Newspaper: [name]
- Date: [date]
- Page: [N] of [total]
- Source image: [filename]

### Text
[transcribed text with {{gap|est=NN}} markers]
```

---

### PASS 2 — Gap Inventory

Review your Pass 1 output. For each `{{ gap }}` marker:

1. Examine the image again at that location
2. Refine the character count estimate
3. Note what partial letterforms or fragments you can see
4. Refine your best guess based on fragments

**Output this pass as an update** — replace each basic gap with an enriched marker (refine the imgbbox if needed):

```
{{ gap | est=NN | imgbbox="x,y,w,h" | fragments="partial_text" [refined guess] }}
```

For example:
```
{{ gap | est=25 | imgbbox="820,2100,400,50" | fragments="Ber...lung" [Versammlung] }}
```

---

### PASS 3 — Cross-Reference and Infill

This pass requires the existing ABBYY OCR (or other traditional OCR) if available. If no reference OCR exists, work from the image alone using contextual inference.

**Instructions:**

1. For each `{{gap}}` marker, examine:
   - The original image at that location (re-examine carefully)
   - The corresponding region in the ABBYY OCR (if provided)
   - Surrounding context in both your Pass 1 text and the ABBYY text
   - Your knowledge of 1890s German, Texas German dialect, and the topic at hand

2. Apply the Fraktur error correction table from `references/fraktur-errors.md` to decode the ABBYY fragments

3. If you can assign a confidence level, promote the gap to an **infill tag** (preserve the imgbbox from the gap):

```
[reconstructed text]^CONFIDENCE^ <!-- {{ infill | est=NN | imgbbox="x,y,w,h" | confidence=LEVEL | region_ocr="raw_abbyy_text" | guess="your reconstruction" }} -->
```

**Confidence levels:**
- `HIGH` — Multiple sources agree, context strongly constrains, result is near-certain
- `MED` — Reasonable inference from partial letterforms + context, probably right
- `LOW` — Educated guess based primarily on context, could easily be wrong
- `VLOW` — Speculative fill to maintain readability, treat as placeholder

4. If you cannot confidently assign a level, leave as a gap with `status=unresolved` and your best guess:

```
{{ gap | est=25 | imgbbox="820,2100,400,50" | fragments="Ber...lung" | status=unresolved [Versammlung] }}
```

The output is **human-readable** (guesses in brackets read naturally in context) AND **machine-parseable** (tags carry metadata for future passes).

**IMPORTANT:** Never leave text blank or use `[unleserlich]`. Every unreadable region gets a best-guess prediction. Even a wild guess from context alone is more useful than a blank — future refinement passes can improve it.

---

## Final Output Assembly

After all three passes, produce the final document:

```markdown
# [Newspaper Name] — [Date] — Page [N]
## OCR Pipeline Output

### Processing Metadata
- Source image: [filename]
- Reference OCR: [source, e.g. "UNT Portal to Texas History / ABBYY"]
- Processing date: [today]
- Pass 1 confidence: [estimated % of text read confidently]
- Total gaps: [count]
- Gaps filled: [count filled in Pass 3]
- Remaining unfilled: [count]

### Statistics
- Estimated total characters on page: [N]
- Characters read with high confidence: [N] ([%])
- Characters infilled at HIGH confidence: [N] ([%])
- Characters infilled at MED confidence: [N] ([%])
- Characters infilled at LOW confidence: [N] ([%])
- Characters infilled at VLOW confidence: [N] ([%])
- Characters in unresolved gaps: [N] ([%])

---

[Final merged text with inline tags as described above]
```

---

## Critical Rules

### DO preserve:
- **Texas German hybrid words**: Stadtmarshall, Dry Goods Haus, Kornhaus, Saloon, Receiver, Farmer, Counties, Lynchversuch, Hotel, Komittee, Rate, Cents
- **English words used in the German text**: unit names (Campbell Guards), street names (Main Str.), legal terms (Receiver), business names
- **Period spellings**: thun, Theil, Noth, Eigenthum, taxiren, stationirt, concurriren, Miethwohnungen, Kenntniß, dasz
- **Germanized place names as printed**: Korpus Christi, Galveston, etc.
- **Dollar amounts and American measurements** as written

### DO NOT:
- Normalize Texas German to standard Hochdeutsch
- Modernize 1890s spelling to post-reform German
- "Correct" English loanwords to German equivalents
- Assume a word is an OCR error when it could be a dialect term — if a word is only 1-2 characters off from a plausible Texas German word, prefer the dialect reading
- Fill gaps with modern German when period phrasing would differ

### Column Interleaving Warning
Newspaper OCR (both ABBYY and your own reading) can accidentally merge adjacent columns. Watch for:
- Sudden topic changes mid-sentence
- Datelines appearing inside unrelated articles
- Names/places that don't match the article's subject
- Two halves of a sentence that don't grammatically connect

When you detect interleaved columns, separate them and note which column each segment belongs to.

---

## Reference Files

Read these BEFORE beginning Pass 1:

| File | Purpose | When to Read |
|------|---------|-------------|
| `references/fraktur-errors.md` | Systematic Fraktur→Latin OCR error patterns | Always, before Pass 1 |
| `references/texas-german.md` | Texas German vocabulary, loanwords, period spelling | Always, before Pass 1 |
| `references/markup-spec.md` | Full metadata tag specification | When assembling final output |
