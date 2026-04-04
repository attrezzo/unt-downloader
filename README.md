# unt-downloader

Automated pipeline to download, OCR-correct, translate, and render PDFs of the
*Bellville Wochenblatt* from the [UNT Portal to Texas History](https://texashistory.unt.edu/).

## Pipeline Stages

```
1. download.py    → Fetch issue pages and raw OCR text from UNT portal
2. ocr_correct.py → Clean and correct OCR artifacts in raw text
3. translate.py   → Translate corrected German text to English via Claude API
4. render_pdf.py  → Compose final bilingual PDF output
```

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/config.example.env config/secrets.env
# Edit secrets.env with your Anthropic API key
```

## Usage

```bash
# Run full pipeline for a date range
python src/pipeline.py --start 1870-01-01 --end 1870-12-31

# Run individual stages
python src/download.py --issue <issue_id>
python src/ocr_correct.py --input <raw_ocr_file>
python src/translate.py --input <corrected_file>
python src/render_pdf.py --input <translated_file>
```

## Configuration

See `config/config.example.env` for all available settings.

Key settings:
- `ANTHROPIC_API_KEY` — required for OCR correction and translation
- `UNT_BASE_URL` — UNT portal base URL
- `TRANSLATE_CHUNK_SIZE` — token chunk size for translation batches (controls cost)
- `OUTPUT_DIR` — where rendered PDFs are written

## Cost Notes

Translation is the most expensive stage. The pipeline extracts plain OCR text
(not HTML) before sending to the API to minimize token usage. See `config/config.example.env`
for tuning options.

## Output

Each issue produces:
- `output/<issue_id>/raw_ocr.txt` — raw text from UNT
- `output/<issue_id>/corrected.txt` — OCR-corrected German
- `output/<issue_id>/translated.txt` — English translation
- `output/<issue_id>/<issue_id>.pdf` — final rendered PDF

## Project Structure

```
unt-downloader/
├── src/
│   ├── pipeline.py       # Orchestrates full run
│   ├── download.py       # UNT portal fetcher
│   ├── ocr_correct.py    # OCR correction via Claude
│   ├── translate.py      # Translation via Claude
│   └── render_pdf.py     # PDF renderer
├── config/
│   └── config.example.env
├── tests/
├── logs/                 # (gitignored)
├── output/               # (gitignored)
├── requirements.txt
└── README.md
```
