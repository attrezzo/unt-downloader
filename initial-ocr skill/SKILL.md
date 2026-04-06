---
name: initial-ocr
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

## Output

A single final document in Markdown with inline metadata tags. The three passes happen internally — only the merged result is returned. See `references/markup-spec.md` for the full tag specification.

**Key principle:** Most text on the page should be high-confidence plain text with no tags. Only uncertain regions get `{{ gap }}` tags. If cross-referencing pushes confidence to cnf >= 0.95, the gap is promoted to plain text and the tag is removed.

---

## The Three Passes

All three passes happen in sequence. Return only the final merged result.

### PASS 1 — Direct Fraktur OCR (high-confidence extraction)

Read the page image directly. This is where most of the text gets captured as plain, untagged, high-confidence output.

**CRITICAL:** Transcribe EVERY word on the page. Never summarize, describe, or skip sections. Writing "[Multiple advertisements, heavily degraded]" is WRONG — you must transcribe each advertisement word by word, using gap tags for the parts you cannot read. Even badly degraded text usually has readable words between the damaged parts.

**Before reading**, internalize:
- `references/fraktur-errors.md` — systematic Fraktur OCR failure modes
- `references/texas-german.md` — Texas German vocabulary, loanwords, period spelling

**Reading a 19th-century newspaper page — layout guide:**

These pages have complex layouts. Read them in this order:

1. **MASTHEAD**: The newspaper title, date, volume/number at the top. Usually spans the full width. Often in large decorative Fraktur.
2. **COLUMNS**: The body text is arranged in columns (typically 4-6).
   - Columns run the **full height** of the page, top to bottom
   - Read each column completely top-to-bottom before moving right
   - A single column may contain multiple articles separated by horizontal rules
3. **ADVERTISEMENTS**: Ads break the regular column flow. Look for:
   - Larger or different fonts (bold, italic, or non-Fraktur type)
   - Decorative borders, boxes, or horizontal rules
   - Centered text (column text is usually justified)
   - Business names, addresses, or product listings
   - Ads may span multiple columns or interrupt column flow
   - When an ad interrupts columns 4-5-6, the column text continues above and below the ad
4. **Reading order** for each column: start at top, read down through articles, tag ads that interrupt with `{{ AdNNN }}`, continue column text below the ad, finish column, move right.

**Instructions:**
1. Identify the page layout: masthead, column count, center-page features (ads, programs, large headlines), visible damage
2. Read left-to-right, column by column, top to bottom within each column
3. Transcribe Fraktur to Latin characters
4. Apply Fraktur error corrections as you read (Tier 1 aggressively, Tiers 2-5 with context)
5. Where text is **confidently readable**, write it directly — no tags needed. This should be the majority of the page.
6. Where text is **illegible or uncertain**, mark with a gap. Do NOT guess yet — just record location and estimated size:
   ```
   {{ gap | est=NN | imgbbox="x,y,w,h" }}
   ```
   `est` = estimated character count. `imgbbox` = approximate pixel bounding box (x,y = top-left, w,h = size). Be generous with the box.

   **Note on `est`:** Fraktur is a proportional typeface. Narrow characters (l, i, t, f, 1) take roughly 30-40% of the width of wide characters (W, M, m, w). A gap region that fits ~12 average characters could hold 8 wide or 18 narrow. Treat `est` as a rough midpoint, not a hard constraint. When guessing in Pass 3, prefer the reading that makes linguistic sense over one that exactly matches the character count.
7. Mark images, illustrations, or engravings:
   ```
   {{ Img | bbox="x,y,w,h" | desc="brief description" }}
   ```
8. Wrap each article/news item/notice in a numbered Column tag:
   ```
   {{ Column001 }}
   ## Headline
   **Dateline,** Date. Article body...
   {{ /Column }}
   ```
9. Wrap each advertisement in a numbered Ad tag:
   ```
   {{ Ad001 }}
   Business name. Products/services. Address.
   {{ /Ad }}
   ```
10. Number Column and Ad tags sequentially per page (001, 002, 003...)
11. Headlines: `##` | Subheads: `###` | Datelines: **bold**
12. Do NOT correct Texas German dialect words or pre-1901 spellings
13. Do NOT translate English loanwords to German
14. Do NOT summarize or describe text — transcribe every word

---

### PASS 2 — Gap Inventory (observation only)

Review your Pass 1 output. For each `{{ gap }}` marker, re-examine the image at that location.

1. Refine the character count estimate and bounding box
2. Record what individual characters you can see — character by character
3. Do NOT guess at meaning — just record the raw letterforms
4. Use `...` for characters you cannot make out at all

The `fragments` field is a **character-level reading** — what the individual letters look like independent of meaning. Not a description, not a guess, not context. Just the raw characters as you see them, including garbled or wrong ones. Use `~` (tilde) for each unreadable character — not `.` which appears in real text.

**Good fragments:**
- `fragments="Ber~~~lung"` — visible B, e, r, 3 unreadable chars, l, u, n, g
- `fragments="(i 77l"` — literally what the characters look like (maybe "little")
- `fragments="Bcrfamm"` — garbled but character-accurate (maybe "Versamml")
- `fragments="nid)t"` — broken ch ligature visible
- `fragments="$ouft~~b"` — $ for Fraktur H, 2 unreadable, then b

**Bad fragments (do NOT do this):**
- `fragments="visible capitals suggest story beginning"` — this is a description
- `fragments="[illegible footer section]"` — this is a label
- `fragments="likely Versammlung"` — this is a guess (guessing is Pass 3)

Update each gap with fragments:
```
{{ gap | est=NN | imgbbox="x,y,w,h" | fragments="raw_characters" }}
```

Example:
```
{{ gap | est=25 | imgbbox="820,2100,400,50" | fragments="Ber~~~lung" }}
{{ gap | est=15 | imgbbox="100,400,200,30" | fragments="$ouft~~b" }}
{{ gap | est=8 | imgbbox="300,800,120,25" | fragments="nid)~~~" }}
```

---

### PASS 3 — Cross-Reference, Guess, and Confidence

This is where guessing happens. For every gap, produce a best guess and assign a confidence score.

**Instructions:**

1. For each `{{ gap }}` marker, examine:
   - The original image at that location (one more careful look)
   - The corresponding region in the ABBYY/portal OCR (if provided)
   - Surrounding context
   - Your knowledge of 1890s German, Texas German dialect, and the article topic

2. Apply the Fraktur error correction table to decode the ABBYY fragments

3. Assign a confidence score and produce your best guess. Remember that `est` is approximate — Fraktur is proportional, so a gap could hold fewer wide characters or more narrow ones than `est` suggests. Prefer the reading that makes linguistic sense over one that exactly matches the character count.

   **If cnf >= 0.95 — PROMOTE TO PLAIN TEXT.** Remove the gap tag entirely. The text is confident enough to stand as untagged output, just like the text from Pass 1. This happens when your reading and the ABBYY OCR strongly agree, context tightly constrains the word, and/or it's a common word with clear fragments.

   **If cnf 0.80–0.94 — auto-resolved gap.** Keep the gap tag with `status=auto-resolved`. Future refinement passes skip these by default:
   ```
   {{ gap | est=12 | imgbbox="450,1200,280,45" | cnf="0.85" | status=auto-resolved | fragments="Verfa~~~ung" | region_ocr="Bcrfaffung" [Verfassung] }}
   ```

   **If cnf < 0.80 — open gap.** Needs future review:
   ```
   {{ gap | est=22 | imgbbox="820,2100,400,50" | cnf="0.25" | fragments="cidrd" | region_ocr="cidrd Rationalitätcn" [verschiedenen Nationalitäten] }}
   ```

4. **Confidence scale (`cnf`):**
   - `0.95–0.99` — Promote to plain text. Remove the gap tag.
   - `0.80–0.94` — High. Add `status=auto-resolved`. Rarely needs review.
   - `0.70–0.79` — Moderate. Fragments match, context fits. Worth reviewing.
   - `0.40–0.69` — Low. Context-based, fragments ambiguous. Should be reviewed.
   - `0.01–0.39` — Speculative. Mostly guessing from context.
   - `0.00` — Pure educated guess. No evidence beyond sentence structure and topic.

5. `region_ocr` MUST contain the exact raw OCR text for this region, uncorrected. This is the most valuable field for future refinement.

6. Every gap that remains MUST have a `[guess]` and a `cnf` score. Never leave a gap without a guess. Even `cnf="0.00"` with a wild guess is more useful than nothing.

---

## Final Output

Return a single document with this structure:

```markdown
# [Newspaper Name] — [Date] — Page [N]
## OCR Pipeline Output

### Processing Metadata
- Source image: [filename]
- Reference OCR: [source, e.g. "UNT Portal to Texas History / ABBYY"]
- Processing date: [today]
- Total gaps remaining: [count]

### Statistics
- Estimated total characters on page: [N]
- Characters with no gap tag: [N] ([%])
- Characters in gaps cnf >= 0.80: [N] ([%])
- Characters in gaps cnf 0.40-0.79: [N] ([%])
- Characters in gaps cnf < 0.40: [N] ([%])

---

[Final merged text — mostly plain text, with {{ gap }} tags only where uncertain]
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
