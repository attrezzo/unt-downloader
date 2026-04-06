# Metadata Markup Specification

Machine-readable tags for tracking OCR provenance, confidence, and reconstruction metadata. Designed so that future AI passes can target only uncertain regions for refinement without re-processing the entire page.

---

## Design Goals

1. **Human-readable**: A person reading the output sees clean text with bracketed uncertainties and confidence markers
2. **Machine-parseable**: HTML comments contain structured metadata that scripts/AI can extract
3. **Refinement-friendly**: Each uncertain region carries enough context for a future AI to make a better guess without seeing the original image
4. **Strippable**: Confident text stands alone if all tags are removed

---

## Tag Types

### 1. Gap Marker (Pass 1-2, unfilled)

Used during Pass 1 and Pass 2 before infill. Marks a region that could not be read.

```
{{gap|est=NN}}
```

**Enriched version (after Pass 2):**
```
{{gap|est=NN|fragments="partial_text"|context="description"}}
```

**Fields:**
- `est` (required): Estimated character count of missing text
- `fragments` (optional): Any partial letterforms visible, as best-guess Latin characters
- `context` (optional): Free-text note on what kind of word/phrase is expected

**Example:**
```
Die {{gap|est=18|fragments="Verfa...ung"|context="noun, likely 'Verfassung' or 'Versammlung'"}} wurde gestern abgehalten.
```

---

### 2. Infill Tag (Pass 3, filled)

Replaces a gap marker after cross-referencing with ABBYY OCR and context.

**Inline display format:**
```
[reconstructed text]^CONFIDENCE^
```

**Full metadata (HTML comment, immediately follows the inline display):**
```
<!-- {{infill|est=NN|confidence=LEVEL|region_ocr="raw_text"|guess="clean_text"|notes="free_text"}} -->
```

**Fields:**
- `est` (required): Character count estimate from Pass 2
- `confidence` (required): `HIGH`, `MED`, `LOW`, or `VLOW`
- `region_ocr` (required): The raw ABBYY OCR text for this region, exactly as it appears in the source — garbled and all. This is the most critical field for future refinement.
- `guess` (required): The clean reconstructed text (same as what appears in brackets)
- `notes` (optional): Free-text notes on reasoning, alternative readings, or flags for future review

**Confidence Definitions:**

| Level | Meaning | Criteria | Future action |
|-------|---------|----------|---------------|
| `HIGH` | Near-certain | Multiple sources agree; strong contextual constraints; common word/phrase | Probably no review needed |
| `MED` | Probable | Partial letterforms match; context fits; reasonable inference | Worth reviewing if dialect corpus improves |
| `LOW` | Plausible guess | Primarily context-based; OCR fragments ambiguous; multiple readings possible | Should be reviewed in future passes |
| `VLOW` | Speculative | Little evidence; filled mainly to maintain readability | Treat as placeholder; definitely review |

**Example:**
```
Die [Versammlung]^MED^ <!-- {{infill|est=18|confidence=MED|region_ocr="Bcrfamm"|guess="Versammlung"|notes="ABBYY has Bcr=Ver, famm=samml, likely Versammlung from context"}} --> wurde gestern abgehalten.
```

---

### 3. Correction Tag (for non-gap corrections)

When you correct a word that was readable but clearly wrong (e.g., obvious Fraktur swap where you're confident of the fix), optionally tag it:

```
corrected_word <!-- {{corrected|original="ocr_reading"|rule="fraktur_bd_swap"}} -->
```

This is optional for common Tier 1 swaps (d/b, f/s) — those are so pervasive that tagging every instance would be noise. Use correction tags for:
- Uncommon or ambiguous corrections
- Cases where the correction changes the meaning
- Words where you're not 100% sure the "correction" is right vs. a dialect term

---

### 4. Column Break Marker

When you detect that OCR has interleaved columns, mark the boundaries:

```
<!-- {{column_break|from=N|to=M}} -->
```

Where N and M are column numbers (1 = leftmost).

---

### 5. Article Boundary Marker

Mark where articles begin and end for structural metadata:

```
<!-- {{article|type="TYPE"|dateline="CITY, DATE"|topic="brief description"}} -->
```

**Types:** `international`, `national`, `texas`, `local`, `program`, `advertisement`, `editorial`, `obituary`

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
- Characters unrecoverable: 555 (3%)

---

<!-- {{article|type="program"|dateline="Bellville"|topic="Deutscher Tag festival program Oct 6"}} -->

## Der Deutsche Tag!

### Große Feier

des Ehrentages der Deutschen am [sechsten Oktober]^MED^ <!-- {{infill|est=16|confidence=MED|region_ocr=""|guess="sechsten Oktober"|notes="date matches program header 'am 6. Oktober' below"}} --> in Austin County.

### Bellville

am 6. Oktober.

veranstaltet von den deutschen Vereinen in Austin County.

### PROGRAMM:

Das Fest beginnt um 10 Uhr Morgens mit einem großen Umzuge, bestehend [aus]^HIGH^ <!-- {{infill|est=3|confidence=HIGH|region_ocr=""|guess="aus"|notes="grammatically required after 'bestehend'"}} -->

### geschmückten Wagen,

darstellend Begebenheiten aus der deutschen Geschichte, oder der [verschiedenen Nationalitäten]^LOW^ <!-- {{infill|est=30|confidence=LOW|region_ocr="cidrd"|guess="verschiedenen Nationalitäten"|notes="ABBYY fragment 'cidrd' unclear, context suggests parade theme descriptions"}} -->

---

<!-- {{article|type="texas"|dateline="Fort Worth, 13. Sept."|topic="Dry goods store burglary"}} -->

**Fort Worth,** 13. Sept. Heute morgen zwischen 2 u. 3 Uhr wurden die Polizeibeamten benachrichtigt, dasz Einbrecher in dem Fort Worth Dry [Goods]^HIGH^ <!-- {{infill|est=5|confidence=HIGH|region_ocr="Try"|guess="Goods"|notes="ABBYY 'Try' is T/D swap + r/o noise = 'Dry'; 'Goods' follows naturally as business name"}} --> Haus, Ecke 14 u. Main Str. an der Arbeit wären.
```

---

## Parsing the Tags

For programmatic extraction, tags follow these regex patterns:

```
# Gap markers (unfilled)
\{\{gap\|est=(\d+)(?:\|fragments="([^"]*)")?(?:\|context="([^"]*)")?\}\}

# Infill markers (in HTML comments)
\{\{infill\|est=(\d+)\|confidence=(HIGH|MED|LOW|VLOW)\|region_ocr="([^"]*)"\|guess="([^"]*)"(?:\|notes="([^"]*)")?\}\}

# Inline display
\[([^\]]+)\]\^(HIGH|MED|LOW|VLOW)\^

# Correction markers
\{\{corrected\|original="([^"]*)"\|rule="([^"]*)"\}\}
```

## Refinement Workflow

To refine tagged output in a future pass:

1. Extract all `{{infill}}` tags with `confidence=LOW` or `confidence=VLOW`
2. For each, provide the future AI with:
   - The `region_ocr` field (raw ABBYY text)
   - The surrounding sentence context (±50 characters each side)
   - The current `guess`
   - Any `notes`
3. The future AI can propose a new guess with updated confidence
4. Replace the tag in place, preserving the original `region_ocr` for audit trail

This allows iterative refinement without re-reading the source image.
