# UNT Archive Downloader — OCR Correction & Translation Toolkit

A reusable pipeline for downloading, correcting, and translating newspaper
collections from the **Portal to Texas History** (texashistory.unt.edu).

Built for **Texas German-language newspapers** typeset in Fraktur — the
Bellville Wochenblatt, Texas Volksblatt, Neu-Braunfelser Zeitung, and similar
German immigrant newspapers held by the German-Texan Heritage Society and
partner institutions. Works for any collection in the UNT Portal.

---

## Files in This Toolkit

| File | Purpose |
|---|---|
| `unt_archive_downloader.py` | **Main entry point.** Configuration wizard, ARK discovery, OCR download, pipeline manager. |
| `unt_ocr_correct.py` | Corrects OCR errors page-by-page using Claude vision API. Handles image preload. |
| `unt_translate.py` | Translates corrected text to English using Claude vision API. |
| `claude_rate_limiter.py` | Shared dual token-bucket rate limiter for parallel Claude API calls. |
| `README.md` | This file. |

---

## Requirements

```powershell
pip install requests
```

An **Anthropic API key** is required for OCR correction and translation.
Set it once as an environment variable:

```powershell
set ANTHROPIC_API_KEY=sk-ant-...
```

---

## Full Pipeline

### Step 1 — Configure your collection (once per collection)

```powershell
python unt_archive_downloader.py --configure
```

An interactive wizard collects everything Claude needs to know:
- Portal title code and ARK scan range
- Publisher, location, language, typeface, date range
- Community description, place names, organization names
- Historical and political context

Saved to `{collection_name}/collection.json`. Edit at any time to improve results.

### Step 2 — Discover all issues

```powershell
python unt_archive_downloader.py --discover
```

Scans the ARK range via IIIF manifest, finds all matching issues,
saves `metadata/all_issues.json`.

### Step 3 — Download OCR text

```powershell
python unt_archive_downloader.py --download-ocr
```

Fetches the Portal's pre-extracted OCR text for every page of every issue.
Saves to `ocr/` — one `.txt` file per issue with `--- Page N of N ---` markers.

### Step 4 — Preload page images (be kind to UNT servers)

```powershell
python unt_archive_downloader.py --preload-images
```

Downloads all page scans from UNT's IIIF server to `images/{ark_id}/page_NN.jpg`
using a parallel thread pool (default 4 workers, polite 1.5s/page crawl rate).
No API key needed. Run **once** before Steps 5 and 6.

Resumable. Failed pages are logged to `images/preload_failures.json`:

```powershell
# Resume after interruption:
python unt_archive_downloader.py --preload-images

# Retry only pages that previously failed:
python unt_archive_downloader.py --preload-images --retry-failed

# Faster download (more workers):
python unt_archive_downloader.py --preload-images --workers 6
```

### Step 5 — Correct OCR with Claude vision

```powershell
python unt_archive_downloader.py --correct --resume
```

For each page, submits the scan image + OCR text to Claude. Claude uses the
image as ground truth to fix Fraktur OCR errors. Output goes to `corrected/`.

Issues are processed in parallel (default 3 workers). A rate limiter prevents
exceeding Anthropic's API limits.

```powershell
# Retry pages that previously failed:
python unt_archive_downloader.py --correct --retry-failed

# Higher parallelism (if you have build tier access):
python unt_archive_downloader.py --correct --resume --tier build --api-workers 6

# Safe serial fallback:
python unt_archive_downloader.py --correct --resume --serial
```

### Step 6 — Translate to English with Claude vision

```powershell
python unt_archive_downloader.py --translate --resume
```

For each page, submits the scan image + corrected OCR + raw OCR to Claude.
Output goes to `translated/` in the **same file structure** as the OCR files,
ready for a future PDF pipeline.

```powershell
# Higher parallelism:
python unt_archive_downloader.py --translate --resume --tier build --api-workers 5

# Single issue test:
python unt_archive_downloader.py --translate --ark metapth1478562
```

### Check progress at any time

```powershell
python unt_archive_downloader.py --status
```

---

## Folder Structure

Each collection is self-contained in its own directory:

```
{collection_name}/
├── collection.json          ← collection config (edit to improve Claude prompts)
├── metadata/
│   └── all_issues.json      ← issue index (ark_id, date, vol, num, pages)
├── ocr/                     ← raw OCR from UNT Portal
│   └── {ark}_vol{v}_no{n}_{date}.txt
├── images/                  ← local page image cache
│   ├── preload_failures.json    ← pages that failed to download
│   └── {ark_id}/
│       ├── page_01.jpg
│       └── ...
├── corrected/               ← Claude-corrected original-language text
│   ├── correction_log.json
│   └── {ark}_vol{v}_no{n}_{date}.txt
└── translated/              ← English translations
    ├── translation_log.json
    └── {ark}_vol{v}_no{n}_{date}.txt
```

All text files share the same internal format — identical header block and
`--- Page N of N ---` markers — so any downstream tool can parse any stage
of the pipeline uniformly:

```
=== COLLECTION TITLE ===
ARK:    metapth1478562
URL:    https://texashistory.unt.edu/ark:/67531/metapth1478562/
Date:   1891-09-17
Volume: 1   Number: 1
Title:  Bellville Wochenblatt. (Bellville, Tex.), Vol. 1, No. 1 ...
============================================================

--- Page 1 of 8 ---
[text]

--- Page 2 of 8 ---
[text]
...
```

---

## Multiple Collections

Each collection is fully independent. Auto-detect works when you `cd` into
a collection directory, or specify explicitly from anywhere:

```powershell
python unt_archive_downloader.py --config-dir bellville_wochenblatt --status
python unt_archive_downloader.py --config-dir neu_braunfelser_zeitung --translate --resume
```

---

## Rate Limiting & Parallelism

### Image preload (`--preload-images`)
Uses Python `ThreadPoolExecutor`. Default 4 workers. Max recommended: 8.
UNT is a public library server — be respectful.

```powershell
python unt_archive_downloader.py --preload-images --workers 6
```

### Claude API calls (`--correct`, `--translate`)
Uses `ThreadPoolExecutor` at the issue level (pages within an issue stay serial).
Governed by `claude_rate_limiter.py` — a dual token-bucket limiter tracking
both requests-per-minute and tokens-per-minute.

| Tier | RPM | TPM | Recommended workers |
|---|---|---|---|
| `default` | 50 | 40,000 | 2–3 |
| `build` | 1,000 | 80,000 | 5–8 |
| `custom` | edit `claude_rate_limiter.py` | — | varies |

Check your actual limits: `console.anthropic.com/settings/limits`

```powershell
python unt_archive_downloader.py --correct --resume --tier build --api-workers 6
python unt_archive_downloader.py --correct --resume --serial   # one at a time fallback
```

---

## Cost Estimate

For a ~100 issue collection at `claude-sonnet-4-6` pricing:

| Stage | Est. cost |
|---|---|
| OCR correction | ~$4–8 |
| Translation | ~$6–12 |
| **Total** | **~$10–20** |

---

## Known Texas German Collections in the UNT Portal

| Title | LCCN | Notes |
|---|---|---|
| Bellville Wochenblatt | sn86088292 | Austin County, 1891–1893, 1909, 1914 |
| Texas Volksblatt | sn86088069 | Industry, Austin County |
| Neu-Braunfelser Zeitung | sn86088194 | New Braunfels, Comal County |
| Galveston Zeitung | sn86088114 | Galveston |
| Texas Staats-Zeitung | sn83045431 | Austin |

Browse more: https://texashistory.unt.edu/explore/collections/

---

## Tips

- **Run `--preload-images` before `--correct` and `--translate`.** Images are
  fetched from local cache during Claude steps — no UNT traffic, faster, more reliable.
- **Edit `collection.json` to improve results.** Add recurring surnames, local
  business names from ads, editorial topics, anything that helps Claude make
  better assumptions.
- **`--resume` is always safe.** Every step checks what's already done.
- **`--retry-failed` targets only broken pages** without reprocessing good ones.
