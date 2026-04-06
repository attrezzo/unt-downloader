---
name: refinement-ocr
description: Refinement pipeline for improving low-confidence OCR gap tags produced by the initial-ocr pipeline. Two modes — text-only refinement (re-evaluates gaps using fragments, reference OCR, and updated vocabulary data without the source image) and image-assisted refinement (crops the source image at each gap's bounding box for targeted re-examination). Use this skill when the user wants to improve OCR quality on already-processed pages, re-evaluate low-confidence gaps, or run a refinement pass after updating the Texas German vocabulary references.
---

# OCR Refinement Pipeline

Two refinement modes for improving `{{ gap }}` tags from the initial OCR pipeline. Both modes update gap tags in-place in the `ai_ocr/` page files.

## When to Use

- After running the initial OCR pipeline (`--correct`)
- After updating vocabulary references (`--update-skill`)
- When you want to improve low-confidence gaps without re-processing entire pages
- When new reference data (ABBYY XML, better Texas German vocabulary) is available

---

## Critical Rules (apply to ALL refinement modes)

**NEVER summarize, describe, or skip text.** Every word must be transcribed. Writing "[illegible classified ads]" or "[damaged text]" is WRONG — use gap tags for parts you cannot read. Even badly degraded text usually has readable words between the damaged regions.

**Every gap MUST use the standard format:**
```
{{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX" | fragments="raw_chars" | region_ocr="raw_ocr" [best guess] }}
```

**Large gaps are valid.** If an entire line or paragraph is damaged, use a single gap tag with a large `est` value and the bbox covering the full region. Do not replace it with a description or placeholder. Record whatever fragments are visible and provide a best guess with appropriate low confidence.

**The `fragments` field is a character-level reading** — what the individual letters literally look like, even if garbled. Use `~` for each unreadable character.
- Good: `fragments="Ber~~~lung"`, `fragments="$ouft~~~b"`, `fragments="nid)t"`
- Bad: `fragments="illegible text"`, `fragments="likely a name"`, `fragments="[damaged]"`

**`region_ocr` MUST contain the exact raw OCR text**, uncorrected. Never clean, correct, or omit it.

**Preserve all existing fields** when updating a gap. Only change `cnf`, `status`, and `[guess]`. Never drop `imgbbox`, `est`, `fragments`, or `region_ocr`.

---

## Newspaper Layout Awareness

When examining gaps in context, be aware of 19th-century newspaper layout:

- **Columns** run the full height of the page, top to bottom. A gap at the bottom of column 1 is contextually related to text above it in the same column, not to text in column 2.
- **Advertisements** interrupt column flow. They use different fonts (bold, italic, non-Fraktur), decorative borders, centered text, or images. When an ad interrupts columns, column text continues above and below the ad.
- **Column interleaving** is a common OCR failure — watch for sudden topic changes or grammatically disconnected sentences that suggest two columns were merged.

---

## Mode 1: Text Refinement (default model: Sonnet)

Re-evaluates gaps using only text: fragments, region_ocr, surrounding context, and the latest reference files. No image needed. Cheap and fast.

**Best for:**
- Gaps with `region_ocr` data (ABBYY had something, may decode better with updated references)
- Gaps with `fragments` (partial letterforms give clues)
- Gaps where surrounding context has improved (earlier gaps on the same page were resolved, providing better sentence context)

**Not useful for:**
- Gaps with cnf="0.00", no fragments, no region_ocr (nothing new to work with)

### Instructions for Claude (text refinement)

You receive a batch of gap tags from OCR-processed newspaper pages, each with ~200 characters of surrounding context.

For each gap:
1. Read the `fragments` and `region_ocr` fields
2. Apply the Fraktur error correction table to decode `region_ocr`
3. Consider the surrounding context (200 chars before and after)
4. Consider the article type and topic from the context
5. Produce an updated guess and confidence score. The `est` field is approximate — Fraktur is proportional (narrow letters like l, i, t take ~30-40% the width of w, M, W). A gap estimated at 12 characters could hold 8 wide or 18 narrow. Prefer the reading that makes linguistic sense over matching the exact character count.

**Update rules:**
- If your new guess is better AND cnf increases → update the gap tag
- If cnf reaches >= 0.95 → promote to plain text (remove the gap tag)
- If cnf reaches >= 0.80 → add `status=auto-resolved`
- If you can't improve on the existing guess → leave the gap unchanged
- Preserve `imgbbox`, `est`, `fragments`, `region_ocr` — only change `cnf` and the `[guess]`

**DO NOT:**
- Replace gaps with descriptions or summaries
- Drop any fields from gap tags
- Correct Texas German dialect words or pre-1901 spellings
- Translate English loanwords to German

**Output format:**
For each gap, return one of:
```
UNCHANGED: gap_id
UPDATED: gap_id | cnf="0.XX" [new guess]
PROMOTED: gap_id [promoted text]
```

Where `gap_id` is the sequential number of the gap in the batch (1, 2, 3...).

---

## Mode 2: Image-Assisted Refinement (default model: Opus)

Sends a cropped region of the source image (from `imgbbox`) along with context text. Targeted and precise — only the damaged region, not the full page.

**Best for:**
- Gaps with cnf 0.01–0.60 that have `fragments` (something visible but unresolved)
- Gaps where the image might reveal details that text cross-referencing missed
- Dense Fraktur where letter-level examination helps

**Skip (not worth Opus cost):**
- Gaps with cnf="0.00" AND no fragments AND no region_ocr (pure context guess, image won't help)
- Gaps already at cnf >= 0.80 (auto-resolved, diminishing returns)

### Bbox Batching

Multiple gaps in the same region of the page should be batched into a single API call:
- Group gaps whose bboxes overlap or are within 100px vertically on the same horizontal band
- Merge the bboxes into one crop (union of all boxes + padding)
- Send one image crop with all gaps listed
- This dramatically reduces cost vs. sending separate crops per gap

### Instructions for Claude (image refinement)

You receive a cropped region of a newspaper page image and one or more gap tags from that region, each with surrounding context.

**CRITICAL:** Transcribe what you see. Never write descriptions like "[degraded text]" or "[unreadable]". If characters are visible, record them in fragments. If nothing is visible, set cnf="0.00" with your best context-based guess.

For each gap:
1. Examine the cropped image carefully at the gap location
2. Look for letterforms, ascenders, descenders, dots, stroke patterns
3. Cross-reference with `fragments`, `region_ocr`, and surrounding context
4. Apply Fraktur error patterns from the reference
5. Produce an updated guess and confidence score

**Same update rules as text refinement:**
- cnf >= 0.95 → promote to plain text
- cnf >= 0.80 → add `status=auto-resolved`
- Can't improve → leave unchanged
- Preserve all fields — only change `cnf`, `status`, and `[guess]`

**DO NOT:**
- Replace gaps with descriptions or summaries
- Write "[illegible]" or "[damaged]" instead of using gap tags
- Correct Texas German or pre-1901 spellings
- Translate English loanwords

**Output format:** Same as text refinement.

---

## Reference Files

These are loaded from the `references/` directory (kept in sync with the initial-ocr skill via `--update-skill`):

| File | Purpose |
|------|---------|
| `references/fraktur-errors.md` | Fraktur error correction table |
| `references/texas-german.md` | Texas German vocabulary (grows over time via --update-skill) |
| `references/markup-spec.md` | Gap tag format specification |

---

## Filtering

Both modes accept:
- **cnf range**: e.g. `--cnf-min 0.0 --cnf-max 0.79` (default for text refinement)
- **date range**: e.g. `--date-from 1891-09-01 --date-to 1891-12-31`
- **ark filter**: e.g. `--ark metapth1478562`

Gaps with `status=auto-resolved` are skipped by default. Use `--include-resolved` to re-evaluate them.
