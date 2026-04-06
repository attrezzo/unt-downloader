# Metadata Markup Specification

Machine-readable tags for tracking OCR provenance, confidence, and reconstruction metadata. Designed so that future AI passes can target only uncertain regions for refinement without re-processing the entire page.

---

## Design Goals

1. **Human-readable**: A person reading the output sees clean text with bracketed guesses and confidence markers
2. **Machine-parseable**: Tags contain structured metadata that scripts/AI can extract
3. **Refinement-friendly**: Each uncertain region carries enough data for a future AI to make a better guess without seeing the original image
4. **Always guessing**: Every unreadable region gets a best-guess prediction, even if speculative. No text is left blank.
5. **Strippable**: Confident text stands alone if all tags are removed; guesses in brackets read naturally

---

## Tag Types

### 1. Gap Marker (unresolved — best guess but low confidence)

Used when text could not be read with confidence. **Every gap MUST include:**
- An estimated character count (`est`)
- A best-guess prediction in square brackets, even if speculative

The guess appears in square brackets inside the tag so the text reads naturally even before refinement.

**Basic gap (Pass 1):**
```
{{ gap | est=NN [best guess] }}
```

**Enriched gap (after Pass 2, with visible fragments):**
```
{{ gap | est=NN | fragments="partial_text" [best guess] }}
```

**Unresolved gap (after Pass 3, cross-referenced but still uncertain):**
```
{{ gap | est=NN | fragments="partial_text" | status=unresolved [best guess] }}
```

**Fields:**
- `est` (required): Estimated character count of missing text
- `fragments` (optional): Any partial letterforms visible, as best-guess Latin characters
- `status` (optional): `unresolved` marks gaps that survived all three passes
- `[best guess]` (required): Always present. The current best prediction of what the text says, in square brackets. Use context, fragments, article topic, and 1890s German knowledge to produce this. Even a wild guess is better than nothing — future passes can improve it.

**Examples:**
```
Die {{ gap | est=12 | fragments="Verfa...ung" [Verfassung] }} wurde gestern abgehalten.

Der Bürgermeister {{ gap | est=22 | fragments="Ber...lung" | status=unresolved [Versammlung] }} erklärte seine Absicht.

Im {{ gap | est=8 [Gasthof] }} an der Hauptstraße fand die Sitzung statt.
```

**IMPORTANT:** `[unleserlich]` is deprecated. Never use it. Every gap gets a best guess, no matter how speculative. If you truly have zero fragments and zero context, guess based on the article topic, surrounding sentence structure, and common 1890s German newspaper phrases. Mark such guesses with `status=unresolved` so future passes know to revisit.

---

### 2. Infill Tag (filled with confidence)

Replaces a gap marker after cross-referencing with ABBYY OCR and context. Used when you have enough evidence to assign a confidence level.

**Inline display format:**
```
[reconstructed text]^CONFIDENCE^
```

**Full metadata (HTML comment, immediately follows the inline display):**
```
<!-- {{ infill | est=NN | confidence=LEVEL | region_ocr="raw_text" | guess="clean_text" | notes="free_text" }} -->
```

**Fields:**
- `est` (required): Character count estimate
- `confidence` (required): `HIGH`, `MED`, `LOW`, or `VLOW`
- `region_ocr` (required): The raw ABBYY OCR text for this region, exactly as it appears in the source — garbled and all. This is the most critical field for future refinement.
- `guess` (required): The clean reconstructed text (same as what appears in brackets)
- `notes` (optional): Free-text notes on reasoning, alternative readings considered

**Confidence Definitions:**

| Level | Meaning | Criteria | Future action |
|-------|---------|----------|---------------|
| `HIGH` | Near-certain | Multiple sources agree; strong contextual constraints; common word/phrase | Probably no review needed |
| `MED` | Probable | Partial letterforms match; context fits; reasonable inference | Worth reviewing if dialect corpus improves |
| `LOW` | Plausible guess | Primarily context-based; OCR fragments ambiguous; multiple readings possible | Should be reviewed in future passes |
| `VLOW` | Speculative | Little evidence; filled mainly to maintain readability | Treat as placeholder; definitely review |

**Example:**
```
Die [Versammlung]^MED^ <!-- {{ infill | est=18 | confidence=MED | region_ocr="Bcrfamm" | guess="Versammlung" | notes="ABBYY has Bcr=Ver, famm=samml" }} --> wurde gestern abgehalten.
```

**Distinction between gap and infill:** A `gap` is a region where you're not confident enough to assign a formal confidence level — the guess is there for readability and as a starting point for future passes. An `infill` is a region where you've done the cross-referencing work and can defend the guess with evidence and a confidence rating.

---

### 3. Correction Tag (for non-gap corrections)

When you correct a word that was readable but clearly wrong (e.g., obvious Fraktur swap where you're confident of the fix), optionally tag it:

```
corrected_word <!-- {{ corrected | original="ocr_reading" | rule="fraktur_bd_swap" }} -->
```

This is optional for common Tier 1 swaps (d/b, f/s) — those are so pervasive that tagging every instance would be noise. Use correction tags for:
- Uncommon or ambiguous corrections
- Cases where the correction changes the meaning
- Words where you're not 100% sure the "correction" is right vs. a dialect term

---

### 4. Article (Column) Marker

Wrap each discrete article, news item, editorial, or notice in a numbered Column tag. Numbers are sequential per page (001, 002, 003...).

```
{{ Column001 }}
## Headline

**Dateline,** Date. Article body text here...
{{ /Column }}
```

Use Column for: news articles, editorials, notices, programs, poetry, masthead, any non-advertising content.

---

### 5. Advertisement Marker

Wrap each advertisement in a numbered Ad tag. Numbers are sequential per page (001, 002, 003...).

```
{{ Ad001 }}
Ad text here. Business name, address, products/services.
{{ /Ad }}
```

Use Ad for: commercial content, classified ads, legal notices paid for by individuals/businesses.

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
- Pass 1 confidence: 72%
- Total gaps: 48
- Gaps filled: 41
- Remaining unfilled: 7

### Statistics
- Estimated total characters on page: 18500
- Characters read with high confidence: 13320 (72%)
- Characters infilled at HIGH: 1850 (10%)
- Characters infilled at MED: 1480 (8%)
- Characters infilled at LOW: 925 (5%)
- Characters infilled at VLOW: 370 (2%)
- Characters in unresolved gaps: 555 (3%)

---

{{ Column001 }}
## Der Deutsche Tag!

### Große Feier

des Ehrentages der Deutschen am [sechsten Oktober]^MED^ <!-- {{ infill | est=16 | confidence=MED | region_ocr="" | guess="sechsten Oktober" | notes="date matches program header 'am 6. Oktober' below" }} --> in Austin County.

### Bellville

am 6. Oktober.

veranstaltet von den deutschen Vereinen in Austin County.

### PROGRAMM:

Das Fest beginnt um 10 Uhr Morgens mit einem großen Umzuge, bestehend [aus]^HIGH^ <!-- {{ infill | est=3 | confidence=HIGH | region_ocr="" | guess="aus" | notes="grammatically required after 'bestehend'" }} -->

### geschmückten Wagen,

darstellend Begebenheiten aus der deutschen Geschichte, oder der {{ gap | est=30 | fragments="cidrd" | status=unresolved [verschiedenen Nationalitäten] }}
{{ /Column }}

{{ Column002 }}
**Fort Worth,** 13. Sept. Heute morgen zwischen 2 u. 3 Uhr wurden die Polizeibeamten benachrichtigt, dasz Einbrecher in dem Fort Worth Dry [Goods]^HIGH^ <!-- {{ infill | est=5 | confidence=HIGH | region_ocr="Try" | guess="Goods" | notes="ABBYY 'Try' is T/D swap + r/o noise = 'Dry'; 'Goods' follows naturally" }} --> Haus, Ecke 14 u. Main Str. an der Arbeit wären.

Die {{ gap | est=15 [Polizeibeamten] }} kamen sofort zur Stelle.
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
# Gap markers (with best guess in brackets)
\{\{\s*gap\s*\|\s*est=(\d+)(?:\s*\|\s*fragments="([^"]*)")?(?:\s*\|\s*status=(\w+))?\s*\[([^\]]*)\]\s*\}\}

# Infill markers (in HTML comments)
\{\{\s*infill\s*\|\s*est=(\d+)\s*\|\s*confidence=(HIGH|MED|LOW|VLOW)\s*\|\s*region_ocr="([^"]*)"\s*\|\s*guess="([^"]*)"(?:\s*\|\s*notes="([^"]*)")?\s*\}\}

# Inline display
\[([^\]]+)\]\^(HIGH|MED|LOW|VLOW)\^

# Correction markers
\{\{\s*corrected\s*\|\s*original="([^"]*)"\s*\|\s*rule="([^"]*)"\s*\}\}

# Article/column structural markers
\{\{\s*(Column|Ad)(\d{3})\s*\}\}     # opening tag
\{\{\s*/(Column|Ad)\s*\}\}            # closing tag

# Page breaks
\[---Page\s*(\d+)---\]
```

## Refinement Workflow

To refine tagged output in a future pass:

1. Extract all `{{ gap }}` tags and `{{ infill }}` tags with `confidence=LOW` or `confidence=VLOW`
2. For each, provide the future AI with:
   - The `region_ocr` field (raw ABBYY text) if available
   - The `fragments` field if available
   - The surrounding sentence (±50 characters each side)
   - The current guess (from `[brackets]` or `guess=` field)
3. The future AI proposes a new guess with updated confidence
4. Replace the tag in place, preserving the original `region_ocr` for audit trail

This allows iterative refinement without re-reading the source image.
