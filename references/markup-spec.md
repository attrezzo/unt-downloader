# Metadata Markup Specification

Machine-readable tags for tracking OCR provenance, confidence, and reconstruction metadata. Designed so that future AI passes can target only uncertain regions for refinement without re-processing the entire page.

---

## Design Goals

1. **Human-readable**: Guesses in brackets read naturally in context
2. **Machine-parseable**: Tags contain structured metadata that scripts/AI can extract
3. **Refinement-friendly**: Each uncertain region carries enough data (bounding box, fragments, OCR source text) for a future AI to improve the guess without re-reading the full page image
4. **Always guessing**: Every unreadable region gets a best-guess prediction. No text is left blank.
5. **Strippable**: Confident text stands alone if all tags are removed

---

## Tag Types

### 1. Gap Marker

The universal tag for any text that is not 100% confident. Covers everything from near-certain reconstructions to wild guesses. The `cnf` field tells you how much to trust it.

**Every gap MUST include:**
- `est` — estimated character count
- `imgbbox` — approximate pixel bounding box in the source image
- `cnf` — confidence score from 0 to 1
- `[best guess]` — the predicted text in square brackets

**Format:**
```
{{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX" [best guess] }}
```

**With optional fields:**
```
{{ gap | est=NN | imgbbox="x,y,w,h" | cnf="0.XX" | status=auto-resolved | fragments="partial_text" | region_ocr="raw_ocr" [best guess] }}
```

**Fields:**
- `est` (required): Estimated character count of the text region. This is approximate — Fraktur is proportional, so narrow characters (l, i, t, f) take ~30-40% the width of wide ones (W, M, m, w). A gap with est=12 could hold 8 wide or 18 narrow characters. Guesses should prioritize linguistic sense over exact character count.
- `imgbbox` (required): Approximate bounding box as `"x,y,w,h"` in pixels (x,y = top-left, w,h = size). Be generous — overestimate to ensure the text is fully contained. Enables future refinement passes to crop just this region instead of resending the full page.
- `cnf` (required): Confidence score from 0 to 1:
  - `0.90–0.99` — High confidence. Multiple sources agree, strong context. Rarely needs review.
  - `0.70–0.89` — Moderate confidence. Fragments match, context fits. Worth reviewing.
  - `0.40–0.69` — Low confidence. Context-based guess, fragments ambiguous. Should be reviewed.
  - `0.01–0.39` — Speculative. Mostly guessing from context. Treat as placeholder.
  - `0.00` — Pure educated guess. No fragments, no OCR source. Derived entirely from context.
  - `1.00` — Never used. Perfect OCR wouldn't be in a gap tag.
- `status` (auto-set): `auto-resolved` when cnf >= 0.80. Indicates the gap is considered resolved and will be skipped by default in future refinement passes. Lower-confidence gaps have no status field and are always candidates for refinement.
- `fragments` (optional): Raw character-level reading of what the letters look like in the image, independent of meaning. Use `...` for unreadable characters. Examples: `"Ber...lung"`, `"$ouft...b"`, `"nid)t"`, `"Bcrfamm"`. NOT a description ("visible capitals") or a guess ("likely Versammlung").
- `region_ocr` (optional): The raw ABBYY/portal OCR text for this region, exactly as it appears — garbled and all. Critical for future refinement.
- `[best guess]` (required): Always present. The current best prediction in square brackets.

**Examples:**
```
Die {{ gap | est=12 | imgbbox="450,1200,280,45" | cnf="0.85" | status=auto-resolved | fragments="Verfa...ung" | region_ocr="Bcrfaffung" [Verfassung] }} wurde gestern abgehalten.

Der {{ gap | est=3 | imgbbox="720,910,60,35" | cnf="0.95" | status=auto-resolved [aus] }} einem großen Umzuge.

Im {{ gap | est=8 | imgbbox="100,950,180,40" | cnf="0.15" [Gasthof] }} an der Hauptstraße fand die Sitzung statt.

Die {{ gap | est=22 | imgbbox="820,2100,400,50" | cnf="0.00" [Bürgerversammlung] }} wurde auf nächste Woche vertagt.
```

**IMPORTANT:** Never leave text blank or use `[unleserlich]`. Every gap gets a best guess, no matter how speculative — set `cnf="0.00"` for pure context guesses.

---

### 2. Correction Tag (for non-gap corrections)

When you correct a word that was readable but clearly wrong (e.g., obvious Fraktur swap where you're confident of the fix), optionally tag it:

```
corrected_word <!-- {{ corrected | original="ocr_reading" | rule="fraktur_bd_swap" }} -->
```

This is optional for common Tier 1 swaps (d/b, f/s) — those are so pervasive that tagging every instance would be noise. Use correction tags for:
- Uncommon or ambiguous corrections
- Cases where the correction changes the meaning
- Words where you're not 100% sure the "correction" is right vs. a dialect term

---

### 3. Article (Column) Marker

Wrap each discrete article, news item, editorial, or notice in a numbered Column tag. Numbers are sequential per page (001, 002, 003...).

```
{{ Column001 }}
## Headline

**Dateline,** Date. Article body text here...
{{ /Column }}
```

Use Column for: news articles, editorials, notices, programs, poetry, masthead, any non-advertising content.

---

### 4. Advertisement Marker

Wrap each advertisement in a numbered Ad tag. Numbers are sequential per page (001, 002, 003...).

```
{{ Ad001 }}
Ad text here. Business name, address, products/services.
{{ /Ad }}
```

Use Ad for: commercial content, classified ads, legal notices paid for by individuals/businesses.

---

### 5. Image Marker

Mark areas of the page that contain images, illustrations, engravings, or other non-text visual content:

```
{{ Img | bbox="x,y,w,h" | desc="brief description" }}
```

**Fields:**
- `bbox` (required): Bounding box in the source image as `"x,y,w,h"` in pixels. Be generous.
- `desc` (required): Brief description of the image content

**Examples:**
```
{{ Img | bbox="200,400,600,800" | desc="engraving of Austin County courthouse" }}

{{ Img | bbox="50,2800,1100,200" | desc="decorative rule separating masthead from articles" }}
```

---

### 6. Column Break Marker

When you detect that OCR has interleaved columns (two unrelated texts merged mid-sentence), mark where the break occurs:

```
<!-- {{ column_break | from=N | to=M }} -->
```

Where N and M are column numbers (1 = leftmost).

---

### 7. Page Break Marker

In multi-page documents, mark page boundaries:

```
[---Page 1---]
```

---

## Full Example

```markdown
# Bellville Wochenblatt — 17. September 1891 — Page 1

## Processing Metadata
- Source image: page_01.jpg
- Reference OCR: UNT Portal to Texas History / ABBYY
- Processing date: 2026-04-05
- Total gaps: 48

### Statistics
- Estimated total characters on page: 18500
- Characters with no gap tag: 13320 (72%)
- Characters in gaps cnf >= 0.80: 3330 (18%)
- Characters in gaps cnf 0.40-0.79: 1295 (7%)
- Characters in gaps cnf < 0.40: 555 (3%)

---

{{ Column001 }}
## Der Deutsche Tag!

### Große Feier

des Ehrentages der Deutschen am {{ gap | est=16 | imgbbox="380,620,300,40" | cnf="0.75" | region_ocr="" [sechsten Oktober] }} in Austin County.

{{ Img | bbox="200,680,500,120" | desc="decorative flourish below program header" }}

### Bellville

am 6. Oktober.

veranstaltet von den deutschen Vereinen in Austin County.

### PROGRAMM:

Das Fest beginnt um 10 Uhr Morgens mit einem großen Umzuge, bestehend {{ gap | est=3 | imgbbox="720,910,60,35" | cnf="0.95" | status=auto-resolved [aus] }}

### geschmückten Wagen,

darstellend Begebenheiten aus der deutschen Geschichte, oder der {{ gap | est=30 | imgbbox="300,1050,500,45" | cnf="0.25" | fragments="cidrd" | region_ocr="cidrd Rationalitätcn" [verschiedenen Nationalitäten] }}
{{ /Column }}

{{ Column002 }}
**Fort Worth,** 13. Sept. Heute morgen zwischen 2 u. 3 Uhr wurden die Polizeibeamten benachrichtigt, dasz Einbrecher in dem Fort Worth Dry {{ gap | est=5 | imgbbox="950,1400,100,35" | cnf="0.92" | status=auto-resolved | region_ocr="Try" [Goods] }} Haus, Ecke 14 u. Main Str. an der Arbeit wären.

Die {{ gap | est=15 | imgbbox="820,1460,280,40" | cnf="0.10" [Polizeibeamten] }} kamen sofort zur Stelle.
{{ /Column }}

{{ Ad001 }}
## C.A. Hermes
Buchdrucker und Herausgeber.
Bellville, Austin County, Texas.
Abonnementspreis: $1.50 per Jahr.
{{ /Ad }}

{{ Ad002 }}
## Dr. F. Reichardt
Zahnarzt.
Office über Haufschild's Store, Bellville.
{{ /Ad }}
```

---

## Parsing the Tags

For programmatic extraction, tags follow these regex patterns:

```
# Gap markers (unified — all uncertain text)
\{\{\s*gap\s*\|\s*est=(\d+)\s*\|\s*imgbbox="([^"]*)"\s*\|\s*cnf="([^"]*)"(?:\s*\|\s*status=(\S+))?(?:\s*\|\s*fragments="([^"]*)")?(?:\s*\|\s*region_ocr="([^"]*)")?\s*\[([^\]]*)\]\s*\}\}

# Image markers
\{\{\s*Img\s*\|\s*bbox="([^"]*)"\s*\|\s*desc="([^"]*)"\s*\}\}

# Correction markers (in HTML comments)
\{\{\s*corrected\s*\|\s*original="([^"]*)"\s*\|\s*rule="([^"]*)"\s*\}\}

# Article/column structural markers
\{\{\s*(Column|Ad)(\d{3})\s*\}\}     # opening tag
\{\{\s*/(Column|Ad)\s*\}\}            # closing tag

# Page breaks
\[---Page\s*(\d+)---\]
```

## Refinement Workflow

To refine tagged output in a future pass:

1. Extract all `{{ gap }}` tags without `status=auto-resolved`, prioritizing low `cnf` values
2. For each, crop the source image using `imgbbox` (saves ~95% token cost vs full page)
3. Provide the future AI with:
   - The cropped image region
   - The `region_ocr` field (raw OCR text) if available
   - The `fragments` field if available
   - The surrounding sentence (±50 characters each side)
   - The current guess from `[brackets]`
4. The future AI proposes a new guess with updated `cnf`
5. Replace the tag in place, preserving the original `region_ocr` for audit trail
