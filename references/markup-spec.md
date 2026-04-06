# OCR Output Markup Specification

Defines all inline tags and metadata markers used in the AI OCR correction
pipeline output.

---

## Gap Markers

Used when text is illegible or unreadable in the source image.

### Basic gap (Pass 1)
```
{{gap|est=NN}}
```
- `est`: estimated character count of missing text
- Used during initial transcription when text cannot be read

### Enriched gap (Pass 2)
```
{{gap|est=NN|fragments="visible_fragments"|context="grammatical and topical notes"}}
```
- `fragments`: any partial letterforms visible (e.g., "Ber...lung")
- `context`: grammatical role, surrounding constraints

### Unresolved gap (after Pass 3)
```
{{gap|est=NN|fragments="..."|context="..."|status=unresolved}}
```
- `status=unresolved`: explicitly marks gaps that could not be filled
  even after cross-referencing with ABBYY OCR

---

## Infill Tags

Used when a gap has been reconstructed with some confidence.

### Inline (human-readable)
```
[reconstructed text]^CONFIDENCE^
```

### Machine-parseable (HTML comment, immediately follows inline)
```
<!-- {{infill|est=NN|confidence=LEVEL|region_ocr="raw_abbyy"|guess="clean_text"|notes="reasoning"}} -->
```

### Fields

| Field | Required | Description |
|-------|----------|-------------|
| est | yes | Original character count estimate |
| confidence | yes | HIGH, MED, LOW, or VLOW |
| region_ocr | yes | Exact raw ABBYY OCR text (no corrections). Empty string if no ABBYY. |
| guess | yes | The clean reconstructed text |
| notes | no | Reasoning, alternative readings considered |

### Confidence Levels

| Level | Meaning |
|-------|---------|
| HIGH | Multiple sources agree, context tightly constrains, common word/phrase |
| MED | Partial letterforms match, context fits, reasonable inference |
| LOW | Primarily context-based, ABBYY fragments ambiguous, multiple readings possible |
| VLOW | Little evidence, filled mainly for readability, treat as placeholder |

---

## Article Boundary Tags

```
<!-- {{article|type="TYPE"|dateline="CITY, DATE"|topic="brief description"}} -->
```

### Article types

| Type | Description |
|------|-------------|
| international | Foreign/wire-service news |
| national | US domestic news |
| texas | Texas state news |
| local | Local community news |
| program | Event program or schedule |
| advertisement | Commercial content |
| editorial | Opinion, editorial |
| obituary | Death notice |
| masthead | Newspaper title/date/volume |
| notice | Legal notice, public announcement |
| poetry | Verse, poetry |
| english_content | Non-German content on page |

---

## Column Break Tags

Used when column interleaving is detected (two stories merged mid-sentence).

```
<!-- {{column_break|from=N|to=M}} -->
```
- `from`: source column number
- `to`: destination column number

---

## Interleaving Warning

When columns cannot be untangled:

```
<!-- {{interleaved|note="columns X and Y could not be separated"}} -->
```

---

## Illegible Marker

The canonical illegible marker (shared across the full pipeline):

```
[unleserlich]
```

With page-absolute bounding box coordinates (when available):
```
[unleserlich bbox=x,y,w,h]
```
- x, y: top-left corner in pixels
- w, h: width and height in pixels
- Coordinates are page-absolute (not column-relative)

In translation output, the equivalent is:
```
[illegible]
```

---

## Text Formatting

| Element | Markdown |
|---------|----------|
| Headline | `## Headline text` |
| Subhead | `### Subhead text` |
| Dateline | `**City, Date**` |
| Paragraph break | blank line |

---

## Output File Header

```markdown
# {Newspaper Name} -- {Date} -- Page {PAGE_NUM}
## OCR Pipeline Output

### Processing Metadata
- Source image: {filename}
- Reference OCR: {source description or "none"}
- Processing date: {date}
- Model: {model name}
- Pipeline version: 1.0

### Page Layout
{brief description}

### Statistics
- Estimated total characters: {N}
- High-confidence direct read: {N} ({%})
- Infilled HIGH: {N} ({%})
- Infilled MED: {N} ({%})
- Infilled LOW: {N} ({%})
- Infilled VLOW: {N} ({%})
- Unrecoverable: {N} ({%})
- Total gaps: {N}
- Gaps filled: {N}
- Gaps remaining: {N}

---

{corrected text with all inline tags}
```
