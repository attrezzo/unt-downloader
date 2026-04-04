# UNT Archive Re-OCR Pipeline — Claude Code Project

Single source of truth for Claude Code. Supersedes all docstrings.

---

## Project intent

Re-OCRs and translates archival images from the UNT Portal to Texas History
(texashistory.unt.edu). Designed so quality can be raised over time.

**Three governing principles:**

1. **Re-processability.** Every word-level confidence score (0–100) is preserved.
   Future tools re-run only low-confidence regions. Never discard scores.
2. **Engine-agnosticism.** OCR engines (ABBYY, Tesseract, Kraken, Claude) are
   interchangeable via the word-token interface. Never couple logic to one engine.
3. **Document-type flexibility.** `collection.json` drives language, script,
   layout. Support new document types by config + preprocessing swap, not rewrites.

Primary collection: **Bellville Wochenblatt** (1891, LCCN sn86088292), 118 issues,
8 pages each, German Fraktur on 35mm microfilm. Reference implementation, not only target.

---

## Confidence tracking — core commitment

Every OCR result carries confidence 0–100. The alignment step classifies each word:

- **HIGH**: all sources agree AND min score ≥ `TESS_CONF_MIN` (40). Written verbatim.
- **LOW**: disagreement or score < 40. Written as `{?provisional?}` in agreed text.
  Added to dispute table with all source readings, scores, line context.

**Planned `--reprocess-low-confidence`** skips HIGH regions, re-runs only disputed
positions. Never discard per-word scores. Never flatten to boolean.

`[unleserlich]` = zero-confidence, examined by all tools, judged unreadable.

---

## File map

```
unt_archive_downloader.py   Orchestrator. Config, discovery, download, subprocess dispatch.
unt_ocr_correct.py          Multi-engine OCR + article segmentation. Primary extension point.
unt_translate.py            Translates corrected/ → English. Smart --resume.
unt_render_pdf.py           Renders translated/ as PDFs. ReportLab only, no API calls.
unt_cost_estimate.py        Model selector + cost confirmation before API use.
claude_rate_limiter.py      Thread-safe dual token-bucket rate limiter.
pricing.json                Hand-maintained $/MTok rates. Edit when prices change.
ocr_pipeline/               Batch-aware preprocessing. Style signatures, confidence data,
                            low-confidence maps. Run: python -m ocr_pipeline --config-path ...
CLAUDE.md                   This file.
README.md                   End-user quickstart.
```

---

## Collection directory layout

All paths relative to collection root (e.g. `bellville_wochenblatt/`).

```
collection.json
metadata/all_issues.json
ocr/{ark}_vol{v}_no{n}_{date}.txt           Raw portal HTML (preserved as-is)
images/{ark_id}/page_01.jpg ... page_NN.jpg
abbyy/{ark}_vol{v}_no{n}_{date}.xml         Optional ABBYY FineReader XML
corrected/{ark}_vol{v}_no{n}_{date}.txt      Best OCR in source language
articles/{ark_id}/pg{NN}_art{NNN}.txt        Segmented articles
articles/{ark_id}/manifest.json
translated/{ark}_vol{v}_no{n}_{date}.txt     English translations
confidence/{ark_id}_page{NN}.json            Per-word confidence (0–100 integers)
artifacts/                                   Batch pipeline working data
    pipeline_log.jsonl  batch_summary.json  style_signatures.json
    low_confidence/{ark_id}_page{NN}.json    Regions flagged for Pass B
pdf/{ark}_vol{v}_no{n}_{date}.pdf
```

Issue filenames: `{ark_id}_vol{vv}_no{nn}_{date}.txt`
Example: `metapth1478562_vol01_no01_1891-09-17.txt`

---

## Shared text file format

**Every file in ocr/, corrected/, translated/ uses this exact format.** Never change it.

```
=== COLLECTION TITLE ===
ARK:    metapth1478562
URL:    https://texashistory.unt.edu/ark:/67531/metapth1478562/
Date:   1891-09-17
Volume: 1   Number: 1
Title:  Bellville Wochenblatt. (Bellville, Tex.), Vol. 1, No. 1 ...
============================================================

--- Page 1 of 8 ---
[text content]
```

- Separator: exactly 60 `=` when written. Detection: `line.startswith('=' * 10)`.
- Page marker regex: `^--- Page (\d+) of (\d+) ---$`
- Encoding: UTF-8 throughout.

---

## Article file format

Written ONLY by `write_article_files()`. File: `pg{NN}_art{NNN}.txt`.

```
ARK:     metapth1478562
ISSUE:   Bellville Wochenblatt Vol. 1 No. 1, 1891-09-17
PAGE:    3  (of 8)
SPANS:   3-4              ← ONLY when article crosses page boundary
TYPE:    article          ← article | advertisement | masthead | notice | poetry
```

`manifest.json` indexes articles: `{ark_id, issue, articles: [{file, page, spans, type, headline}]}`

---

## Pipeline commands

```bash
python unt_archive_downloader.py --configure           # interactive setup
python unt_archive_downloader.py --discover            # find issue ARKs
python unt_archive_downloader.py --download-ocr        # fetch portal HTML
python unt_archive_downloader.py --preload-images      # cache IIIF scans
python unt_archive_downloader.py --correct --resume    # OCR correction
python unt_archive_downloader.py --translate --resume  # translate to English
python unt_archive_downloader.py --render-pdf --resume # produce PDFs
python unt_archive_downloader.py --status              # progress report
```

Common flags: `--ark`, `--date-from`, `--date-to`, `--resume`, `--api-workers N`,
`--tier default|build|custom`, `--serial`, `--retry-failed`, `--delay N`.

Direct invocation: `python unt_ocr_correct.py --config-path collection.json --resume`

Orchestrator passes flags via `run_worker()` → `subprocess.run()`.

---

## collection.json schema

```json
{
  "title_name": "Bellville Wochenblatt", "lccn": "sn86088292",
  "publisher": "C.A. Hermes", "pub_location": "Bellville, Texas",
  "date_range": "1891-1892", "language": "German", "typeface": "Fraktur",
  "source_medium": "35mm microfilm", "layout_type": "newspaper",
  "permalink": "https://texashistory.unt.edu/explore/titles/.../",
  "title_keyword": "bellville",
  "community_desc": "...", "place_names": "...", "organizations": "...",
  "historical_context": "...", "subject_notes": "...",
  "anthropic_api_key": "sk-ant-...", "claude_model": "claude-sonnet-4-6"
}
```

- `typeface`: "Fraktur" (case-insensitive) includes Fraktur error table in prompts.
- `language`: drives Tesseract language selection + translation prompts.
- `title_keyword`: filters IIIF manifests in --discover.
- `layout_type`: planned dispatch for Stage 3/9. Values: `newspaper`, `letter`,
  `ledger`, `photograph`, `handwritten_document`.
- `claude_model`: overrides `CLAUDE_MODEL` constant.
- `anthropic_api_key`: fallback if `ANTHROPIC_API_KEY` env var unset.

---

## Key constants

```python
# unt_ocr_correct.py
ILLEGIBLE = "[unleserlich]"                    # THE canonical marker
TESS_CONF_MIN = 40                             # below = always disputed
TESS_PSM_A = "--psm 6 --oem 1 --dpi 300"      # uniform text block
TESS_PSM_B = "--psm 4 --oem 1 --dpi 300"      # single column
TESS_LANG_PRIORITY = ["deu_frak+deu", "deu_frak", "deu", "eng"]
CLAUDE_MODEL = "claude-sonnet-4-6"             # overridden by collection.json

# unt_translate.py
BUDGET_EXCEEDED_PREFIX = "[BUDGET EXCEEDED: PAGE "
DEFAULT_MAX_OUTPUT_TOKENS = 32_000

# claude_rate_limiter.py
TIER_DEFAULT = {"rpm": 50,  "tpm": 40_000}     # free / low usage
TIER_BUILD   = {"rpm": 1000, "tpm": 80_000}    # after $5 spend
```

---

## Word token interface — OCR engine contract

Every engine returns `list[dict]`. Alignment accepts any mix automatically.

```python
{"text": str, "conf": int,    # 0–100. Use ILLEGIBLE + conf=0 for unreadable.
 "source": str,                # "abbyy"|"tess_a"|"tess_b"|"kraken"|custom
 "left": int, "top": int, "right": int, "bottom": int}  # COLUMN-LOCAL px coords
```

**Adding a new engine:**
1. Write `my_engine_tokens(col_img, source_tag) -> list` returning tokens above.
2. Add `HAS_MY_ENGINE` boolean via try/except import (same pattern as HAS_KRAKEN).
3. In `process_page()`: `if HAS_MY_ENGINE: sources["my_engine"] = my_engine_tokens(strip, "my_engine")`
4. No other changes. Alignment and arbitration are automatic.

---

## OCR correction pipeline — stages

`process_page()` runs stages 1–8 in sequence. Each is a swap point.

| Stage | Function(s) | What it does |
|-------|-------------|-------------|
| 1 ABBYY | `parse_abbyy_page(xml, page_index)` → (tokens, blocks) | Parse legacy XML. Called in `process_issue()`, passed in. |
| 2 Preprocess | `preprocess_image(img_gray)` → enhanced | CLAHE(2.5, 8×8) + medianBlur(3). Swap: deskew, Sauvola. |
| 3 Layout | `detect_content_bounds()` → (l,t,r,b); `detect_columns_from_image()` → [(x1,x2),...] | Trim borders (thresh=60). Bottom-30% vertical projection, find_peaks. |
| 4 Boundary | `compare_boundaries(opencv_cols, abbyy_gutters, snap_tolerance=20, expected_cols)` | 3-pass: snap→insert→prune. Only when ABBYY present. |
| 5 Tesseract | `tesseract_tokens(col_img, lang, config, tag)` × 2 passes | PSM-6 + PSM-4. `image_to_data` for per-word conf+bbox. conf<10 → ILLEGIBLE. |
| 6 Kraken | `kraken_tokens(col_img)` | Optional. Lazy-loads model. Returns [] on error. |
| 7 Alignment | `align_sources(sources)` → `split_agree_dispute(aligned)` | Anchor priority: abbyy>tess_a>tess_b>kraken. 45px tolerance. Agreed/disputed split. |
| 8 Claude | `arbitrate_with_claude(...)` | Image + agreed text + dispute table (≤300). Resolves `{?...?}`. Safety net strips leftovers. |

**Stages 9–10 (in `process_issue()`):**
- `segment_page()` — Claude segments corrected text into articles/ads.
- `stitch_all_pages()` — sequential cross-page stitching. Do not parallelize.
- `write_article_files()` — ONLY writer to `articles/`.

### Key data structures

**AlignedWord:** `{tokens: {source: tok|None}, agree: bool, consensus: str, dispute_reason: str}`

**Dispute table entry:** `{top, left, provisional, readings: {src: word}, confs: {src: int}, reason, line_context}`

**`{?...?}` contract:** Written by `split_agree_dispute()`, explained to Claude in
`arbitrate_with_claude()`, cleaned by `re.sub(r'\{\?([^?}]*)\?\}', r'\1', result)`.
Update all three together.

---

## Translation pipeline

- `audit_translated_file()` classifies each page. Untranslated = HTML, BUDGET_EXCEEDED,
  FAILED, MISSING, ERROR markers, or empty.
- `process_issue()`: audit → `get_source_pages()` for needed pages → `call_claude()`
  (all pages in one call for cross-page consistency) → merge → `write_translated_file()`.
- `call_claude()` handles max_tokens: writes BUDGET_EXCEEDED with embedded OCR for retry.
- `write_translated_file()` is THE ONLY writer to `translated/`. Always full merged dict.
- `get_source_pages()`: priority corrected/ → stripped ocr/ → empty. Never raw HTML.

---

## Other subsystems

**Downloader** (`unt_archive_downloader.py`): `run_worker()` dispatches via `subprocess.run()`.
`discover_issues()` scans ARKs, filters by `title_keyword`. `find_config_path()` walks up from CWD.

**Cost estimate** (`unt_cost_estimate.py`): `choose_model_and_confirm()` shows live model list
with pricing from `pricing.json`, estimates cost, prompts y/N. Calls `sys.exit(0)` on decline.

**Rate limiter** (`claude_rate_limiter.py`): `ClaudeRateLimiter(rpm, tpm, safety_factor=0.80)`.
`acquire(est_tokens)` blocks until slots available. `record_usage(in, out)` updates stats.
`limiter_from_tier("default"|"build"|"custom")`.

**PDF renderer** (`unt_render_pdf.py`): ReportLab only, no API calls, no network.
Reads `translated/`, embeds scan images as fallback for failed pages.

---

## Adding a new document type

1. Create `collection.json` with appropriate `language`, `typeface`, `source_medium`, `layout_type`.
2. New preprocessing function alongside `preprocess_image()`, dispatch by `layout_type`.
3. New layout detection in `process_page()` (Kraken BLLA for handwriting, ruling detection for ledgers).
4. **Write separate prompt builders** — not conditionals in existing ones. Fraktur table is newspaper-specific.
5. New `SEGMENTATION_PROMPT` item types for the document kind.

Orchestrator and translate need no changes for new document types.

---

## Dependencies

```bash
pip install requests pytesseract opencv-python-headless pillow scipy numpy reportlab
apt-get install tesseract-ocr tesseract-ocr-deu
# Better Fraktur: wget tessdata_best/deu.traineddata → /usr/share/tesseract-ocr/5/tessdata/
# Optional: pip install kraken && kraken models download 10.0.0
# ABBYY XML: request from ana.krahmer@unt.edu, place in {collection}/abbyy/
```

---

## Coding rules

**Never discard per-word confidence data.** Keep 0–100 integers. TESS_CONF_MIN=40 is tuneable.

**Never break --resume.** All steps resumable and idempotent. Change output format → update resume detection.

**ocr/ stores raw portal HTML.** Strip only at read time via `strip_ocr_html()`.

**corrected/ is the only translation input.** `get_source_pages()` enforces this.

**Translated files are patched.** `write_translated_file()` is sole writer, receives full merged dict.

**OCR engines are independent.** Engine logic in engine function only. `align_sources()` is source-agnostic.

**No hardcoded expected_cols.** Always from `collection.json`.

**System prompts are document-type-specific.** Separate builders, not conditionals.

**`{?...?}` dispute markers:** contract between `split_agree_dispute()`, `arbitrate_with_claude()`,
and the `re.sub` safety net. Update all three together.

**=== separator:** exactly 60 `=` when written. Detection: `startswith('=' * 10)`.

**`[unleserlich]`** is THE ONLY illegible marker. Translated equivalent: `[illegible]`.

**`stitch_all_pages()` is sequential.** Page 3→4 merge feeds into 4→5. Do not parallelize.

**Engine exceptions → `[]`.** Failed engine degrades quality, doesn't halt pipeline.

**requests in downloader only.** OCR/translate scripts use stdlib `urllib.request`.

---

## Known design decisions

- **Masthead zone:** Column detection uses bottom 30% to avoid multi-column mastheads on page 1.
- **ABBYY as one vote:** Trusted for structural layout (gutter positions), not character recognition.
- **Translation batches all pages:** One call per issue for terminology consistency.
  max_tokens → BUDGET_EXCEEDED markers with embedded OCR for --resume retry.
- **Kraken lazy-loads:** `_KRAKEN_MODEL = None`, populated on first `kraken_tokens()` call.

---

## Contacts

- UNT Digital Projects (ABBYY XML): ana.krahmer@unt.edu
- Anthropic pricing: platform.claude.com/docs/en/about-claude/pricing
- Tesseract tessdata_best: github.com/tesseract-ocr/tessdata_best
- Kraken: github.com/mittagessen/kraken

| Collection | LCCN |
|---|---|
| Bellville Wochenblatt | sn86088292 |
| Texas Volksblatt | sn86088069 |
| Neu-Braunfelser Zeitung | sn86088194 |
| Galveston Zeitung | sn86088114 |
| Texas Staats-Zeitung | sn83045431 |
