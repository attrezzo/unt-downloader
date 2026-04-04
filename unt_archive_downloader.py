#!/usr/bin/env python3
"""
UNT Portal to Texas History — Archive Downloader & Pipeline Manager
====================================================================
A general-purpose tool for downloading, OCR-correcting, and translating
collections from the Portal to Texas History (texashistory.unt.edu).

Originally built for the Bellville Wochenblatt (German-Texas newspapers),
but works with any newspaper or serial collection in the UNT Portal.

This script is the entry point for the full pipeline:

  STEP 1 — Configure collection (interactive, runs once):
    python unt_archive_downloader.py --configure

  STEP 2 — Discover all issue ARKs:
    python unt_archive_downloader.py --discover

  STEP 3 — Download OCR text:
    python unt_archive_downloader.py --download-ocr

  STEP 4 — Preload page images (kind to UNT servers):
    python unt_archive_downloader.py --preload-images

  STEP 5 — Correct OCR with Claude vision:
    python unt_archive_downloader.py --correct

  STEP 6 — Translate to English with Claude vision:
    python unt_archive_downloader.py --translate

  Or check progress at any time:
    python unt_archive_downloader.py --status

  Shortcuts:
    python unt_archive_downloader.py --all         (steps 2+3)
    python unt_archive_downloader.py --resume      (adds resume to any step)

COLLECTION CONFIG:
  On first run (--configure or any step if no config exists), you will be
  asked to describe the collection. Answers are saved to:
    {collection_dir}/collection.json

  This file is read by unt_ocr_correct.py and unt_translate.py to build
  accurate Claude prompts. Edit it at any time to refine results.

REQUIREMENTS:
  pip install requests
  Anthropic API key for steps 5-6 (set ANTHROPIC_API_KEY or use --api-key)
"""

import sys, os, json, time, re, argparse, subprocess, textwrap, threading
import requests
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# UNT Portal base URL (never changes)
# ---------------------------------------------------------------------------
BASE_URL    = "https://texashistory.unt.edu"
CRAWL_DELAY = 1.2   # seconds between requests — be kind to UNT servers

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "UNT-Archive-Researcher/1.0 (Texas history research)"
})

# These are set after collection config is loaded
OUTPUT_DIR    = None
METADATA_DIR  = None
OCR_DIR       = None
PDF_DIR       = None
CORRECTED_DIR = None
TRANSLATED_DIR= None
IMAGES_DIR    = None
CONFIG_PATH   = None

def init_paths(collection_dir: Path):
    """Set all path globals from the collection root directory."""
    global OUTPUT_DIR, METADATA_DIR, OCR_DIR, PDF_DIR
    global CORRECTED_DIR, TRANSLATED_DIR, IMAGES_DIR, CONFIG_PATH
    OUTPUT_DIR     = collection_dir
    METADATA_DIR   = collection_dir / "metadata"
    OCR_DIR        = collection_dir / "ocr"
    PDF_DIR        = collection_dir / "pdf"
    CORRECTED_DIR  = collection_dir / "corrected"
    TRANSLATED_DIR = collection_dir / "translated"
    IMAGES_DIR     = collection_dir / "images"
    CONFIG_PATH    = collection_dir / "collection.json"

# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------
def item_url(ark):     return f"{BASE_URL}/ark:/67531/{ark}/"
def ocr_url(ark, pg):  return f"{BASE_URL}/ark:/67531/{ark}/m1/{pg}/ocr/"
def pdf_url(ark):      return f"{BASE_URL}/ark:/67531/{ark}/pdf/"
def manifest_url(ark): return f"{BASE_URL}/ark:/67531/{ark}/manifest/"

# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def ask(prompt: str, default: str = "", required: bool = False) -> str:
    """Prompt user for input with an optional default value."""
    suffix = f" [{default}]" if default else ""
    while True:
        answer = input(f"  {prompt}{suffix}: ").strip()
        if not answer and default:
            return default
        if answer or not required:
            return answer
        print("    (required — please enter a value)")


def ask_multiline(prompt: str) -> str:
    """Prompt for multi-line input. Empty line finishes."""
    print(f"  {prompt}")
    print("  (Enter a blank line to finish)")
    lines = []
    while True:
        line = input("  > ")
        if not line.strip() and lines:
            break
        if line.strip():
            lines.append(line.strip())
    return " ".join(lines)


# ---------------------------------------------------------------------------
# Global configuration (project-level, not per-collection)
# ---------------------------------------------------------------------------

GLOBAL_CONFIG_PATH = Path(__file__).parent / "config.json"


def load_global_config() -> dict:
    """Load config.json from project root. Returns empty dict if missing."""
    if GLOBAL_CONFIG_PATH.exists():
        try:
            return json.loads(GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_global_config(config: dict):
    """Write config.json to project root."""
    GLOBAL_CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    print(f"✓ Global config saved → {GLOBAL_CONFIG_PATH}\n")


def configure_global(existing: dict = None) -> dict:
    """Interactive wizard for global (non-collection) settings."""
    e = existing or load_global_config()

    print()
    print("─" * 72)
    print("GLOBAL SETTINGS  (shared across all collections)")
    print("─" * 72)

    # ── API key ──────────────────────────────────────────────────────────
    print("\nYour Anthropic API key is needed for the --correct and --translate steps.")
    print("It will be stored in config.json (git-ignored). Leave blank to set later")
    print("via the ANTHROPIC_API_KEY environment variable or --api-key flag.\n")
    existing_key = e.get("anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")
    key_display  = f"...{existing_key[-6:]}" if len(existing_key) > 6 else ""
    api_key      = ask("Anthropic API key (sk-ant-...)", key_display)
    if api_key == key_display and existing_key:
        api_key = existing_key

    # ── Default model ────────────────────────────────────────────────────
    default_model = e.get("claude_model", "claude-sonnet-4-6")
    chosen_model  = default_model

    if api_key:
        print()
        print("─" * 72)
        print("DEFAULT MODEL")
        print("─" * 72)
        print("Choose the default Claude model for --correct and --translate.")
        print("You can override per-collection in collection.json.\n")
        try:
            from unt_cost_estimate import configure_model
            chosen_model = configure_model(api_key, current_model=default_model)
        except ImportError:
            print("  (unt_cost_estimate.py not found — keeping default model)")
        except Exception as exc:
            print(f"  (Model lookup failed: {exc} — keeping default)")
    else:
        print("\n  (API key not set — skipping model selection; default: claude-sonnet-4-6)")

    # ── Rate limit tier ──────────────────────────────────────────────────
    tier = ask(
        "Default rate-limit tier (default / build / custom)",
        e.get("tier", "default")
    )
    if tier not in ("default", "build", "custom"):
        tier = "default"

    config = {
        "anthropic_api_key": api_key,
        "claude_model":      chosen_model,
        "tier":              tier,
    }

    # Preserve any extra keys the user may have added manually
    for k, v in e.items():
        if k not in config:
            config[k] = v

    print()
    print("─" * 72)
    print("GLOBAL CONFIG SUMMARY")
    print("─" * 72)
    print(f"  API key  : {'...' + api_key[-6:] if len(api_key) > 6 else '(not set)'}")
    print(f"  Model    : {chosen_model}")
    print(f"  Tier     : {tier}")
    print()

    confirm = ask("Save global settings?", "yes")
    if confirm.lower() not in ("yes", "y"):
        print("  (Skipped — global settings not saved)")
        return e  # return previous

    save_global_config(config)
    return config


# ---------------------------------------------------------------------------
# Interactive collection configuration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# UNT metadata lookup — called before the wizard to pre-populate defaults
# ---------------------------------------------------------------------------

def _fetch_title_metadata(title_code: str) -> dict:
    """
    Fetch metadata from UNT's title-level endpoints using the title code.

    Sources tried in order:
      1. identifiers.json  → LCCN, OCLC, partner ARKs, collection code
      2. KBART.txt         → title name, date range, first/last issue dates
      3. First issue IIIF manifest → publisher, language, ARK range start,
                                     page count, first canvas image service URL

    Returns a dict of discovered values (any field may be absent if the
    endpoint failed or didn't contain it). All values are strings.
    """
    discovered = {"title_code": title_code}
    base = "https://texashistory.unt.edu"

    headers = {"User-Agent": "UNT-Archive-Researcher/1.0 (Texas history research)"}

    # ── 1. identifiers.json ───────────────────────────────────────────────
    try:
        url = f"{base}/explore/titles/{title_code}/identifiers.json"
        req = requests.get(url, headers=headers, timeout=20)
        if req.status_code == 200:
            ids = req.json()
            # Structure: list of {type, value} dicts  OR  dict of type→value
            if isinstance(ids, list):
                for entry in ids:
                    t = entry.get("type", "").lower()
                    v = str(entry.get("value", "")).strip()
                    if t == "lccn"  and v: discovered["lccn"]          = v
                    if t == "oclc"  and v: discovered["oclc"]          = v
                    if t == "ark"   and v: discovered["first_ark"]     = v
                    if t in ("collection", "code") and v:
                        discovered["collection_id"] = v
            elif isinstance(ids, dict):
                for k, v in ids.items():
                    kl = k.lower()
                    if "lccn"  in kl and v: discovered["lccn"]          = str(v)
                    if "oclc"  in kl and v: discovered["oclc"]          = str(v)
                    if "ark"   in kl and v: discovered["first_ark"]     = str(v)
    except Exception:
        pass

    # ── 2. KBART.txt ──────────────────────────────────────────────────────
    try:
        url = f"{base}/explore/titles/{title_code}/KBART.txt"
        req = requests.get(url, headers=headers, timeout=20)
        if req.status_code == 200:
            lines = req.text.splitlines()
            if len(lines) >= 2:
                hdrs = lines[0].split("\t")
                vals = lines[1].split("\t")
                row  = dict(zip(hdrs, vals))
                if row.get("publication_title"):
                    discovered["title_name"] = row["publication_title"].strip()
                if row.get("date_first_issue_online"):
                    discovered["date_first"] = row["date_first_issue_online"].strip()
                if row.get("date_last_issue_online"):
                    discovered["date_last"]  = row["date_last_issue_online"].strip()
                if row.get("publisher_name"):
                    discovered["publisher"]  = row["publisher_name"].strip()
                if row.get("coverage_notes"):
                    discovered["coverage"]   = row["coverage_notes"].strip()
    except Exception:
        pass

    # Build date_range string if we got both ends
    if discovered.get("date_first") and discovered.get("date_last"):
        y1 = discovered["date_first"][:4]
        y2 = discovered["date_last"][:4]
        discovered["date_range"] = f"{y1}–{y2}" if y1 != y2 else y1

    # ── 3. IIIF manifest of first known issue ─────────────────────────────
    # Try to find a first ARK from identifiers, or scan a small range
    first_ark = discovered.get("first_ark", "")

    # If identifiers.json gave us an ARK list, it may be the title-level ARK
    # not an issue ARK — strip any path and try it as an issue manifest
    if first_ark and "/" in first_ark:
        first_ark = first_ark.rstrip("/").split("/")[-1]

    if first_ark and first_ark.startswith("metapth"):
        try:
            url = f"{base}/ark:/67531/{first_ark}/manifest/"
            req = requests.get(url, headers=headers, timeout=20)
            if req.status_code == 200:
                mf = req.json()
                _parse_manifest_into(mf, first_ark, discovered)
        except Exception:
            pass

    return discovered


def _parse_manifest_into(mf: dict, ark_id: str, discovered: dict):
    """Extract useful fields from a IIIF manifest into discovered dict."""
    label = mf.get("label", "")
    if label and "title_name" not in discovered:
        # Strip the volume/issue suffix to get just the title name
        # e.g. "Bellville Wochenblatt. (Bellville, Tex.), Vol. 1, No. 1 ..."
        # → "Bellville Wochenblatt"
        clean = re.sub(r"\s*[\.,]\s*\(.*", "", label).strip()
        if clean:
            discovered["title_name"] = clean

    # Metadata array: look for publisher, language, location, date
    for entry in mf.get("metadata", []):
        lbl = entry.get("label", "").lower()
        val = entry.get("value", "")
        if isinstance(val, list): val = val[0] if val else ""
        val = str(val).strip()
        if not val:
            continue
        if "publisher" in lbl and "publisher" not in discovered:
            discovered["publisher"] = val
        if "language" in lbl and "language" not in discovered:
            discovered["language"] = val
        if "location" in lbl and "pub_location" not in discovered:
            discovered["pub_location"] = val
        if "date" in lbl and "date_first" not in discovered:
            discovered["date_first"] = val

    # ARK range: record the numeric part so we can suggest a scan start
    m = re.search(r"metapth(\d+)", ark_id)
    if m:
        n = int(m.group(1))
        discovered["ark_scan_start_hint"] = n - 5   # start a little before

    # Page count from first canvas
    seqs = mf.get("sequences", [])
    if seqs:
        canvases = seqs[0].get("canvases", [])
        if canvases and "pages_typical" not in discovered:
            discovered["pages_typical"] = len(canvases)


def _lookup_by_ark(ark_id: str) -> dict:
    """
    Alternative entry point: look up via a known issue ARK rather than title code.
    Fetches the IIIF manifest and extracts what it can.
    Returns discovered dict.
    """
    discovered = {"first_ark": ark_id}
    base    = "https://texashistory.unt.edu"
    headers = {"User-Agent": "UNT-Archive-Researcher/1.0 (Texas history research)"}

    try:
        url = f"{base}/ark:/67531/{ark_id}/manifest/"
        req = requests.get(url, headers=headers, timeout=20)
        if req.status_code == 200:
            mf = req.json()
            _parse_manifest_into(mf, ark_id, discovered)
    except Exception:
        pass

    return discovered


def _print_discovered(d: dict):
    """Pretty-print what was auto-discovered from UNT."""
    fields = [
        ("Title",        d.get("title_name")),
        ("LCCN",         d.get("lccn")),
        ("OCLC",         d.get("oclc")),
        ("Publisher",    d.get("publisher")),
        ("Location",     d.get("pub_location")),
        ("Date range",   d.get("date_range")),
        ("Language",     d.get("language")),
        ("First ARK",    d.get("first_ark")),
        ("ARK start ≈",  str(d["ark_scan_start_hint"]) if "ark_scan_start_hint" in d else None),
    ]
    any_found = any(v for _, v in fields)
    if not any_found:
        print("  (No metadata retrieved — check your identifier and network connection)")
        return
    for label, val in fields:
        if val:
            print(f"  {label:<14}: {val}")


# ---------------------------------------------------------------------------
# Interactive collection configuration
# ---------------------------------------------------------------------------

CONFIGURE_INTRO = """
╔══════════════════════════════════════════════════════════════════════════╗
║         UNT Portal to Texas History — Archive Downloader                ║
║         OCR Correction & Translation Toolkit                            ║
╚══════════════════════════════════════════════════════════════════════════╝

This toolkit downloads newspaper and serial collections from the Portal to
Texas History (texashistory.unt.edu), corrects OCR errors using Claude's
vision API, and translates non-English text to English.

IT IS DESIGNED FOR TEXAS GERMAN LANGUAGE MATERIALS.
The Claude prompts include specialized context for:
  • Fraktur typeface OCR error correction
  • 19th/early 20th century German vocabulary and syntax
  • Texas-German community geography and institutions
  • German immigrant newspapers in Central Texas

It will work for other languages and collections, but performs best for
German-language Texas newspapers — the Wochenblatt family, Zeitung titles,
and similar German-Texan serials held by the German-Texan Heritage Society.
"""

LOOKUP_PROMPT = """
─────────────────────────────────────────────────────────────────────────
STEP 1 — IDENTIFY YOUR COLLECTION
─────────────────────────────────────────────────────────────────────────
Provide one identifier and the script will look up the rest automatically.

You can use any of these:

  Portal title code  →  found in the URL of the title's page
                         e.g. https://texashistory.unt.edu/explore/titles/t02903/
                         → title code is  t02903

  LCCN               →  Library of Congress Control Number
                         e.g. sn86088292

  A known issue ARK  →  the ARK of any single issue you've already found
                         e.g. metapth1478562

Enter just the value — no URL, no label prefix.
"""


def run_configure(existing: dict = None) -> dict:
    """
    Configuration wizard — two phases:

    Phase 1  Ask for one identifier, fetch everything UNT knows, display it
             all at once. User accepts with Enter or types 'edit' to correct
             any field individually.

    Phase 2  Ask only for things UNT cannot provide:
               • ARK scan range (start / end numbers)
               • Claude context (community, history, subjects, place names)
               • Output folder name
    """
    e = existing or {}
    print(CONFIGURE_INTRO)
    print(LOOKUP_PROMPT)

    # ─── Phase 1: single identifier → bulk lookup ─────────────────────────
    identifier = ask(
        "Title code, LCCN, or issue ARK",
        e.get("title_code") or e.get("lccn") or e.get("first_ark", ""),
        required=True
    ).strip()

    # Accept pasted URLs — extract the useful fragment
    if identifier.startswith("http"):
        m = re.search(r"titles/(t\d+)", identifier)
        if m: identifier = m.group(1)
        else:
            m = re.search(r"(metapth\d+)", identifier)
            if m: identifier = m.group(1)

    identifier = identifier.lower().strip()
    print(f"\nFetching metadata for '{identifier}' from texashistory.unt.edu ...")

    discovered = {}
    if re.match(r"^t\d+$", identifier):
        discovered = _fetch_title_metadata(identifier)
        discovered["title_code"] = identifier
    elif re.match(r"^sn\d+", identifier) or re.match(r"^\d{8,}$", identifier):
        discovered["lccn"] = identifier
    elif re.match(r"^metapth\d+$", identifier):
        discovered = _lookup_by_ark(identifier)
        discovered["first_ark"] = identifier

    # Merge with any existing config (existing wins only if discovered is empty)
    for k, v in e.items():
        if k not in discovered or not discovered[k]:
            discovered[k] = v

    # Build default output dir from title
    title_name = discovered.get("title_name", "")
    if title_name and not discovered.get("output_dir_name"):
        discovered["output_dir_name"] = re.sub(
            r"[^\w\-]", "_", title_name.lower().replace(" ", "_")
        )[:40]

    # Derive keyword from first word of title
    if title_name and not discovered.get("title_keyword"):
        discovered["title_keyword"] = title_name.split()[0].lower()

    # ─── Display everything discovered ────────────────────────────────────
    print()
    print("─" * 72)
    print("DISCOVERED COLLECTION METADATA")
    print("─" * 72)

    # Fields in display order: (label, config_key, width)
    display_fields = [
        ("Title",           "title_name"),
        ("Title code",      "title_code"),
        ("LCCN",            "lccn"),
        ("OCLC",            "oclc"),
        ("Collection code", "collection_id"),
        ("Publisher",       "publisher"),
        ("Location",        "pub_location"),
        ("Date range",      "date_range"),
        ("Language",        "language"),
        ("Typeface",        "typeface"),
        ("Source medium",   "source_medium"),
        ("First ARK",       "first_ark"),
        ("Match keyword",   "title_keyword"),
        ("Output folder",   "output_dir_name"),
    ]

    for label, key in display_fields:
        val = discovered.get(key, "")
        marker = "  ✓" if val else "  —"
        print(f"{marker}  {label:<18}: {val or '(not found)'}")

    print()
    accept = ask(
        "Accept all discovered values? (yes / no to edit field by field)",
        "yes"
    )

    if accept.lower() not in ("yes", "y"):
        # ── Field-by-field edit of discovered data ─────────────────────────
        print("\nEdit each field — press Enter to keep the current value.\n")
        for label, key in display_fields:
            # typeface and source_medium have sensible hardcoded defaults
            default_fallbacks = {
                "typeface":      "Fraktur",
                "source_medium": "35mm microfilm",
                "language":      "German",
            }
            current = discovered.get(key) or default_fallbacks.get(key, "")
            discovered[key] = ask(label, current)

    # Ensure defaults for typeface / source_medium if still blank
    if not discovered.get("typeface"):      discovered["typeface"]      = "Fraktur"
    if not discovered.get("source_medium"): discovered["source_medium"] = "35mm microfilm"
    if not discovered.get("language"):      discovered["language"]      = "German"

    title_name    = discovered.get("title_name", "")
    title_code    = discovered.get("title_code", "")
    title_keyword = discovered.get("title_keyword", "")

    # ─── Phase 2: things only the researcher knows ────────────────────────
    print()
    print("─" * 72)
    print("ARK SCAN RANGE")
    print("─" * 72)
    print("UNT does not expose the full ARK range for a title via its API.")
    print("Provide the numeric portion of a known ARK near the start of the")
    print("collection, and a reasonable upper bound. Include a small buffer")
    print("on each side — the scanner skips non-matching ARKs automatically.\n")

    # Use discovered hint if available
    ark_hint_start = discovered.get("ark_scan_start_hint") or discovered.get("ark_scan_start", "")
    ark_hint_end   = discovered.get("ark_scan_end", "")
    if ark_hint_start and not ark_hint_end:
        ark_hint_end = int(ark_hint_start) + 150

    if ark_hint_start:
        print(f"  Hint from manifest: first issue ARK ≈ metapth{ark_hint_start}")
        print( "  (adjust if needed — add a few before and a generous buffer after)\n")

    ark_start = ask(
        "First ARK number to scan (digits only)",
        str(e.get("ark_scan_start") or ark_hint_start or ""),
        required=True
    )
    ark_end = ask(
        "Last ARK number to scan",
        str(e.get("ark_scan_end") or ark_hint_end or ""),
        required=True
    )

    print()
    print("─" * 72)
    print("CLAUDE CONTEXT  (researcher knowledge — improves OCR & translation)")
    print("─" * 72)
    print("Leave blank to skip any field. You can edit collection.json later.\n")

    community_desc = ask_multiline(
        "Community description (who read this, where they lived, their background):"
    ) or e.get("community_desc", "")

    place_names = ask(
        "Place names to preserve untranslated (comma-separated)",
        e.get("place_names", "")
    )
    organizations = ask(
        "Organization names to preserve (comma-separated)",
        e.get("organizations", "")
    )
    historical_context = ask_multiline(
        "Historical/political/economic context:"
    ) or e.get("historical_context", "")

    subject_notes = ask(
        "Recurring subjects (e.g. cotton prices, church notices, court records)",
        e.get("subject_notes", "")
    )

    print()
    print("─" * 72)
    print("DOCUMENT LAYOUT")
    print("─" * 72)
    print("These settings control column detection and article segmentation.")
    print("Defaults are correct for most UNT newspaper collections.\n")

    layout_type = ask(
        "Layout type (newspaper / letter / ledger / photograph / handwritten_document)",
        e.get("layout_type", "newspaper")
    )

    default_cols = "5" if layout_type == "newspaper" else "1"
    expected_cols_raw = ask(
        "Columns per page (e.g. 5 for a 5-column newspaper, 1 for a letter)",
        e.get("expected_cols", default_cols)
    )
    try:
        expected_cols = max(1, int(expected_cols_raw))
    except (ValueError, TypeError):
        expected_cols = 5

    print()
    print("─" * 72)
    print("OUTPUT FOLDER")
    print("─" * 72)
    default_dir = discovered.get("output_dir_name") or re.sub(
        r"[^\w\-]", "_", title_name.lower().replace(" ", "_")
    )[:40]
    output_dir_name = ask(
        "Output folder name (created in current directory)",
        default_dir
    )

    # ─── Assemble and confirm ─────────────────────────────────────────────
    config = {
        "title_name":         discovered.get("title_name", ""),
        "lccn":               discovered.get("lccn", ""),
        "oclc":               discovered.get("oclc", ""),
        "title_code":         title_code,
        "permalink":          f"https://texashistory.unt.edu/explore/titles/{title_code}/",
        "collection_id":      discovered.get("collection_id", ""),
        "ark_scan_start":     int(ark_start),
        "ark_scan_end":       int(ark_end),
        "title_keyword":      title_keyword.lower(),
        "publisher":          discovered.get("publisher", ""),
        "pub_location":       discovered.get("pub_location", ""),
        "date_range":         discovered.get("date_range", ""),
        "language":           discovered.get("language", "German"),
        "typeface":           discovered.get("typeface", "Fraktur"),
        "source_medium":      discovered.get("source_medium", "35mm microfilm"),
        "community_desc":     community_desc,
        "place_names":        place_names,
        "organizations":      organizations,
        "historical_context": historical_context,
        "subject_notes":      subject_notes,
        "layout_type":        layout_type,
        "expected_cols":      expected_cols,
        "output_dir_name":    output_dir_name,
    }

    print()
    print("─" * 72)
    print("COLLECTION CONFIGURATION")
    print("─" * 72)
    print(f"  Collection : {config['title_name']}")
    print(f"  Title code : {config['title_code']}  |  LCCN: {config['lccn'] or '—'}  |  OCLC: {config['oclc'] or '—'}")
    print(f"  ARK range  : metapth{ark_start} – metapth{ark_end}  (keyword: '{title_keyword}')")
    print(f"  Language   : {config['language']}  ({config['typeface']})")
    print(f"  Publisher  : {config['publisher'] or '—'}")
    print(f"  Dates      : {config['date_range'] or '—'}")
    print(f"  Output dir : {output_dir_name}/")
    print()

    confirm = ask("Save and continue? (yes/no)", "yes")
    if confirm.lower() not in ("yes", "y"):
        print("Configuration cancelled.")
        sys.exit(0)

    return config


def load_or_configure(config_path: Path, force: bool = False) -> dict:
    """
    Load existing collection.json or run the configuration wizard.
    Returns the config dict.
    """
    if config_path.exists() and not force:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        print(f"Loaded collection config: {config['title_name']}")
        print(f"  Output dir : {config['output_dir_name']}/")
        print(f"  ARK range  : metapth{config['ark_scan_start']} – metapth{config['ark_scan_end']}")
        print(f"  Language   : {config['language']}  ({config.get('typeface','')})")
        print()
        return config
    else:
        config = run_configure(
            existing=(json.loads(config_path.read_text()) if config_path.exists() else None)
        )
        # We don't know the final config_path yet if this is first run
        return config


def save_config(config: dict, config_path: Path):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    print(f"✓ Configuration saved → {config_path}\n")


# ---------------------------------------------------------------------------
# ARK probing
# ---------------------------------------------------------------------------
def probe_ark(ark_id: str, title_keyword: str) -> dict | None:
    """
    Probe a single ARK via its IIIF manifest.
    Returns issue metadata dict if it matches title_keyword, else None.
    """
    try:
        r = SESSION.get(manifest_url(ark_id), timeout=20)
        if r.status_code != 200:
            return None
        data  = r.json()
        label = data.get("label", "")
        if title_keyword.lower() not in label.lower():
            return None
        # Page count
        pages = 8
        seqs  = data.get("sequences", [])
        if seqs:
            canvases = seqs[0].get("canvases", [])
            if canvases:
                pages = len(canvases)
        # Vol / number
        vol, num = "", ""
        m = re.search(r"Vol\.?\s*(\d+),\s*No\.?\s*(\d+)", label, re.I)
        if m:
            vol, num = m.group(1), m.group(2)
        # Date
        date_str = ""
        for entry in data.get("metadata", []):
            lbl = entry.get("label", "")
            val = entry.get("value", "")
            if isinstance(val, list): val = val[0] if val else ""
            if "date" in lbl.lower() and val:
                date_str = str(val); break
        if not date_str:
            dm = re.search(
                r"(January|February|March|April|May|June|July|August|"
                r"September|October|November|December)\s+\d+,\s+\d{4}", label)
            if dm: date_str = dm.group(0)
        return {
            "ark_id":     ark_id,
            "ark_url":    item_url(ark_id),
            "full_title": label,
            "volume":     vol,
            "number":     num,
            "date":       date_str,
            "pages":      pages,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def discover_issues(config: dict) -> list:
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    index_path = METADATA_DIR / "all_issues.json"

    start   = int(config["ark_scan_start"])
    end     = int(config["ark_scan_end"])
    keyword = config["title_keyword"]
    total   = end - start

    print(f"Scanning metapth{start}–metapth{end} ({total} ARKs)")
    print(f"Matching label keyword: '{keyword}'")
    print(f"Estimated time: ~{int(total * CRAWL_DELAY / 60)} min\n")

    found = []
    for idx, n in enumerate(range(start, end)):
        ark_id = f"metapth{n}"
        if idx % 10 == 0:
            print(f"  [{idx:>3}/{total}]  {ark_id}  found: {len(found)}")
        result = probe_ark(ark_id, keyword)
        if result:
            found.append(result)
            print(f"  ✓ {ark_id}  Vol.{result['volume']} No.{result['number']}  {result['date']}")
        time.sleep(CRAWL_DELAY)

    found.sort(key=lambda x: x.get("date", ""))

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(found, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Found {len(found)} issues — saved to {index_path}")
    return found


# ---------------------------------------------------------------------------
# OCR download
# ---------------------------------------------------------------------------
def _fetch_one_ocr_page(ark_id: str, page: int, pages: int) -> tuple:
    """
    Fetch OCR text for a single page. Returns (page, total_pages, text).

    The UNT portal /ocr/ endpoint returns a full HTML page containing the OCR
    text inside <div id="ocr-text">. We store the COMPLETE HTML response on
    disk so it is available for future uses (layout analysis, formatting
    reconstruction, etc.). HTML stripping happens later in the correction step
    (unt_ocr_correct.py) before any text is submitted to Claude.

    Each call uses its own session so threads don't share connections.
    """
    url = ocr_url(ark_id, page)
    try:
        s = requests.Session()
        s.headers.update(SESSION.headers)
        r = s.get(url, timeout=30)
        text = r.text.strip() if r.status_code == 200 else ""
    except Exception as e:
        text = f"[error: {e}]"
    return page, pages, text


def _download_issue_ocr(task: dict, page_workers: int) -> dict:
    """
    Download OCR text for all pages of one issue using a nested thread pool.
    Returns a result dict with keys: fname, status, kb.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ark_id   = task["ark_id"]
    fname    = task["fname"]
    pages    = task["pages"]
    header   = task["header"]
    out_path = task["out_path"]

    # Submit all pages in parallel
    page_results = {}
    with ThreadPoolExecutor(max_workers=min(page_workers, pages)) as ex:
        futures = {
            ex.submit(_fetch_one_ocr_page, ark_id, pg, pages): pg
            for pg in range(1, pages + 1)
        }
        for future in as_completed(futures):
            try:
                pg, total, text = future.result()
            except Exception as e:
                pg    = futures[future]
                total = pages
                text  = f"[error: {e}]"
            page_results[pg] = text

    # Reassemble in page order
    lines = list(header)
    for pg in range(1, pages + 1):
        text = page_results.get(pg, "[missing]")
        lines += [f"--- Page {pg} of {pages} ---", text or "[no OCR]", ""]

    out_path.write_text("\n".join(lines), encoding="utf-8")
    kb = out_path.stat().st_size // 1024
    return {"fname": fname, "status": "ok", "kb": kb}


def download_all_ocr(config: dict, resume: bool = True, workers: int = 8):
    """
    Download OCR text for all issues.

    By default skips issues whose .txt file already exists (resume=True).
    Pass resume=False (via --force-ocr) to re-download everything.

    Parallelism operates at two levels:
      • Issue level  — `workers` issues processed simultaneously
      • Page level   — all pages within each issue fetched simultaneously

    Args:
        config:   collection config dict
        resume:   if True (default), skip already-downloaded files
        workers:  number of parallel issue threads (default 8)
    """
    index_path = METADATA_DIR / "all_issues.json"
    if not index_path.exists():
        sys.exit("No issue index. Run --discover first.")
    with open(index_path, encoding="utf-8") as f:
        issues = json.load(f)

    title = config["title_name"]
    OCR_DIR.mkdir(parents=True, exist_ok=True)

    # Build task list, separating skips up front
    tasks   = []
    skipped = 0
    for issue in issues:
        ark_id   = issue["ark_id"]
        vol      = str(issue.get("volume", "?")).zfill(2)
        num      = str(issue.get("number", "?")).zfill(2)
        date     = re.sub(r"[^\w\-]", "-", issue.get("date", "unknown"))
        pages    = int(issue.get("pages", 8))
        fname    = f"{ark_id}_vol{vol}_no{num}_{date}.txt"
        out_path = OCR_DIR / fname

        if resume and out_path.exists() and out_path.stat().st_size > 200:
            skipped += 1
            continue

        header = [
            f"=== {title.upper()} ===",
            f"ARK:    {ark_id}",
            f"URL:    {item_url(ark_id)}",
            f"Date:   {issue.get('date', '')}",
            f"Volume: {issue.get('volume', '')}   Number: {issue.get('number', '')}",
            f"Title:  {issue.get('full_title', '')}",
            "=" * 60,
            "",
        ]
        tasks.append({
            "ark_id":   ark_id,
            "fname":    fname,
            "pages":    pages,
            "header":   header,
            "out_path": out_path,
        })

    total = len(issues)
    to_dl = len(tasks)
    # Pages-per-issue is fixed (usually 8); total concurrent requests = workers × pages
    avg_pages      = int(sum(t["pages"] for t in tasks) / max(to_dl, 1))
    max_concurrent = workers * avg_pages
    est_sec        = int(to_dl * avg_pages * 0.3 / max(workers, 1))  # ~0.3s/page
    est_min, est_s = divmod(est_sec, 60)

    print(f"OCR download — {total} issues total")
    print(f"  Skipping       : {skipped}  (already downloaded)")
    print(f"  To download    : {to_dl}  issues × ~{avg_pages} pages")
    print(f"  Issue workers  : {workers}")
    print(f"  Max concurrent : ~{max_concurrent} requests  (pages fetched in parallel)")
    print(f"  Est. time      : ~{est_min}m {est_s:02d}s\n")

    if not tasks:
        print("✓ Nothing to download.")
        return

    print_lock      = threading.Lock()
    completed_count = [0]

    def on_done(result: dict):
        with print_lock:
            completed_count[0] += 1
            n = completed_count[0]
            if result["status"] == "ok":
                print(f"  [{n:>3}/{to_dl}] ✓  {result['fname']}  ({result['kb']} KB)")
            else:
                print(f"  [{n:>3}/{to_dl}] ✗  {result['fname']}  {result.get('error','')}")

    ok_count  = 0
    err_count = 0

    # Page-level concurrency: each issue gets min(workers, pages) page threads.
    # We pass page_workers = workers so each issue uses the full pool width
    # for its pages — issues themselves are also parallelised by the outer pool.
    page_workers = workers

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(_download_issue_ocr, task, page_workers): task
            for task in tasks
        }
        for future in as_completed(future_map):
            try:
                result = future.result()
            except Exception as e:
                task   = future_map[future]
                result = {"fname": task["fname"], "status": "error", "error": str(e)}
            on_done(result)
            if result["status"] == "ok":
                ok_count += 1
            else:
                err_count += 1

    print(f"\n✓ OCR download complete.")
    print(f"  Downloaded : {ok_count}")
    if skipped:
        print(f"  Skipped    : {skipped}  (already existed)")
    if err_count:
        print(f"  Errors     : {err_count}")
    print(f"  Output     : {OCR_DIR}")


# ---------------------------------------------------------------------------
# PDF download (optional)
# ---------------------------------------------------------------------------
def download_all_pdfs(resume: bool = False):
    index_path = METADATA_DIR / "all_issues.json"
    if not index_path.exists():
        sys.exit("No issue index. Run --discover first.")
    with open(index_path, encoding="utf-8") as f:
        issues = json.load(f)

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading PDFs for {len(issues)} issues ...\n")

    for i, issue in enumerate(issues):
        ark_id   = issue["ark_id"]
        vol      = str(issue.get("volume", "?")).zfill(2)
        num      = str(issue.get("number", "?")).zfill(2)
        date     = re.sub(r"[^\w\-]", "-", issue.get("date", "unknown"))
        fname    = f"{ark_id}_vol{vol}_no{num}_{date}.pdf"
        out_path = PDF_DIR / fname

        if resume and out_path.exists() and out_path.stat().st_size > 50_000:
            print(f"[{i+1:02d}/{len(issues)}] SKIP  {fname}")
            continue

        print(f"[{i+1:02d}/{len(issues)}] {ark_id}  {pdf_url(ark_id)}")
        try:
            r = SESSION.get(pdf_url(ark_id), timeout=180, stream=True)
            r.raise_for_status()
            with open(out_path, "wb") as fh:
                for chunk in r.iter_content(65536):
                    fh.write(chunk)
            print(f"         → {fname}  ({out_path.stat().st_size / 1048576:.1f} MB)")
        except Exception as e:
            print(f"         ✗ {e}")
        time.sleep(CRAWL_DELAY * 3)

    print(f"\n✓ PDFs complete → {PDF_DIR}")


# ---------------------------------------------------------------------------
# Delegate to worker scripts
# ---------------------------------------------------------------------------
def run_worker(script_name: str, extra_args: list):
    """Run unt_ocr_correct.py or unt_translate.py as a subprocess."""
    script = Path(__file__).parent / script_name
    if not script.exists():
        print(f"Error: {script_name} not found at {script}")
        print(f"Make sure {script_name} is in the same folder as this script.")
        sys.exit(1)
    cmd = [sys.executable, str(script)] + extra_args
    print(f"Launching: {' '.join(cmd)}\n")
    result = subprocess.run(cmd)
    sys.exit(result.returncode)


# ---------------------------------------------------------------------------
# Status report
# ---------------------------------------------------------------------------
def show_status(config: dict):
    index_path = METADATA_DIR / "all_issues.json"
    if not index_path.exists():
        print("No issue index found. Run --discover first.")
        return

    with open(index_path, encoding="utf-8") as f:
        issues = json.load(f)

    title  = config.get("title_name", "Collection")
    script = Path(__file__).name

    print(f"\n{title} — Pipeline Status")
    print("=" * 60)
    print(f"Total issues : {len(issues)}")
    print(f"Output dir   : {OUTPUT_DIR}\n")

    stages = [
        ("Raw OCR",       OCR_DIR),
        ("Images cached", IMAGES_DIR),
        ("ABBYY XML",     OUTPUT_DIR / "abbyy"),
        ("Corrected OCR", CORRECTED_DIR),
        ("Articles",      OUTPUT_DIR / "articles"),
        ("Translated",    TRANSLATED_DIR),
        ("PDFs",          OUTPUT_DIR / "pdf"),
    ]

    for label, folder in stages:
        if label == "Images cached":
            # Count individual page images
            count = sum(1 for iss in issues
                        for p in range(1, int(iss.get("pages", 8)) + 1)
                        if (folder / iss["ark_id"] / f"page_{p:02d}.jpg").exists())
            total_pages = sum(int(i.get("pages", 8)) for i in issues)
            bar_unit = max(1, total_pages // 50)
            filled = min(50, count // bar_unit)
            bar = "█" * filled + "░" * (50 - filled)
            pct = int(count / total_pages * 100) if total_pages else 0
            print(f"  {label:<16} {count:>4}/{total_pages}pp  [{bar}]  {pct}%")
        elif label == "ABBYY XML":
            count = len(list(folder.glob("*.xml"))) if folder.exists() else 0
            note  = "(optional — contact ana.krahmer@unt.edu)" if count == 0 else f"{count} file(s)"
            print(f"  {label:<16} {note}")
        elif label == "Articles":
            # Count ark_id subdirectories that have at least one article file
            if folder.exists():
                count = sum(1 for d in folder.iterdir()
                            if d.is_dir() and any(d.glob("*_art*.txt")))
            else:
                count = 0
            bar = "█" * count + "░" * max(0, len(issues) - count)
            pct = int(count / len(issues) * 100) if issues else 0
            print(f"  {label:<16} {count:>3}/{len(issues)}     [{bar}]  {pct}%")
        elif label == "PDFs":
            count = len(list(folder.glob("*.pdf"))) if folder.exists() else 0
            bar = "█" * count + "░" * max(0, len(issues) - count)
            pct = int(count / len(issues) * 100) if issues else 0
            print(f"  {label:<16} {count:>3}/{len(issues)}     [{bar}]  {pct}%")
        else:
            count = len(list(folder.glob("*.txt"))) if folder.exists() else 0
            bar   = "█" * count + "░" * max(0, len(issues) - count)
            pct   = int(count / len(issues) * 100) if issues else 0
            print(f"  {label:<16} {count:>3}/{len(issues)}     [{bar}]  {pct}%")

    print()
    print("Next steps:")
    steps = []
    if not any(OCR_DIR.glob("*.txt") if OCR_DIR.exists() else []):
        steps.append(f"  python {script} --download-ocr")
    img_count = sum(1 for iss in issues
                    for p in range(1, int(iss.get("pages", 8)) + 1)
                    if IMAGES_DIR and (IMAGES_DIR / iss["ark_id"] / f"page_{p:02d}.jpg").exists())
    total_pages = sum(int(i.get("pages", 8)) for i in issues)
    if img_count < total_pages:
        steps.append(f"  python {script} --preload-images")
    if not any(CORRECTED_DIR.glob("*.txt") if CORRECTED_DIR.exists() else []):
        steps.append(f"  python {script} --correct --resume")
    articles_dir = OUTPUT_DIR / "articles"
    articles_done = sum(1 for d in articles_dir.iterdir()
                        if d.is_dir() and any(d.glob("*_art*.txt"))) \
                    if articles_dir.exists() else 0
    if articles_done < len(issues):
        steps.append(f"  python {script} --correct --resume  # also generates articles/")
    if not any(TRANSLATED_DIR.glob("*.txt") if TRANSLATED_DIR.exists() else []):
        steps.append(f"  python {script} --translate --resume")
    pdf_dir = OUTPUT_DIR / "pdf"
    if not any(pdf_dir.glob("*.pdf") if pdf_dir.exists() else []):
        steps.append(f"  python {script} --render-pdf --resume")
    for s in steps:
        print(s)
    if not steps:
        print("  All pipeline stages complete!")


# ---------------------------------------------------------------------------
# Find config — walk up from cwd looking for collection.json,
# or accept --config-dir argument
# ---------------------------------------------------------------------------
def find_config_path(config_dir_arg: str | None) -> Path:
    """
    Determine where collection.json lives / should live.
    Priority:
      1. --config-dir argument
      2. collection.json in current directory
      3. collection.json in any immediate subdirectory
      4. Prompt user to configure (returns a path in a new directory)
    """
    if config_dir_arg:
        return Path(config_dir_arg) / "collection.json"

    # Check current dir
    if Path("collection.json").exists():
        return Path("collection.json")

    # Check one level of subdirectories
    for subdir in sorted(Path(".").iterdir()):
        if subdir.is_dir() and (subdir / "collection.json").exists():
            return subdir / "collection.json"

    # Not found — will be created after configure
    return Path("collection.json")   # placeholder; configure() will set the real path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(
        description="UNT Archive Downloader — download, correct, and translate Texas history collections",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""
        examples:
          python unt_archive_downloader.py --configure
          python unt_archive_downloader.py --discover
          python unt_archive_downloader.py --download-ocr --resume
          python unt_archive_downloader.py --preload-images
          python unt_archive_downloader.py --correct --resume
          python unt_archive_downloader.py --translate --resume
          python unt_archive_downloader.py --translate --ark metapth1478562
          python unt_archive_downloader.py --status
          python unt_archive_downloader.py --config-dir my_collection/ --status
        """)
    )

    # Pipeline steps
    p.add_argument("--configure",      action="store_true",
                   help="Run the collection configuration wizard (required on first use)")
    p.add_argument("--discover",       action="store_true",
                   help="Scan ARK range to find all issues")
    p.add_argument("--download-ocr",   action="store_true",
                   help="Download raw OCR text (skips already-cached files by default)")
    p.add_argument("--force-ocr",      action="store_true",
                   help="Re-download OCR even if already cached locally")
    p.add_argument("--download-pdf",   action="store_true",
                   help="Download full-issue image PDFs (large)")
    p.add_argument("--preload-images", action="store_true",
                   help="Download all page images to local cache (run before --correct)")
    p.add_argument("--correct",        action="store_true",
                   help="Correct OCR errors using Claude vision API")
    p.add_argument("--translate",      action="store_true",
                   help="Translate to English using Claude vision API")
    p.add_argument("--render-pdf",     action="store_true",
                   help="Render translated issues as newspaper-style PDFs (no API calls)")
    p.add_argument("--status",         action="store_true",
                   help="Show pipeline progress for this collection")
    p.add_argument("--all",            action="store_true",
                   help="Run --discover + --download-ocr")

    # Shared options
    p.add_argument("--config-dir",     default=None,
                   help="Path to collection directory (default: auto-detect)")
    p.add_argument("--resume",         action="store_true",
                   help="Skip already-completed files")

    # Claude API options (passed through to worker scripts)
    p.add_argument("--api-key",        default=None,
                   help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    p.add_argument("--ark",            default=None,
                   help="Process only this ARK ID")
    p.add_argument("--date-from",      default=None,
                   help="Only process issues on/after this date (YYYY-MM-DD)")
    p.add_argument("--date-to",        default=None,
                   help="Only process issues on/before this date (YYYY-MM-DD)")
    p.add_argument("--page-delay",     type=float, default=None,
                   help="Seconds between page API calls")
    p.add_argument("--issue-delay",    type=float, default=None,
                   help="Seconds between issues")
    p.add_argument("--retry-failed",   action="store_true",
                   help="(--correct only) Retry pages marked as failed")
    p.add_argument("--columns",        type=int, default=5,
                   help="Newspaper columns for --render-pdf (default: 5)")
    p.add_argument("--max-output-tokens", type=int, default=32000,
                   help="Claude max output tokens for --translate (default: 32000; "
                        "increase to 48000-64000 if you see [BUDGET EXCEEDED] markers)")
    p.add_argument("--workers",        type=int, default=8,
                   help="Parallel threads for --download-ocr and --preload-images (default: 8). "
                        "OCR pages are fetched in parallel within each issue too, so total "
                        "concurrent requests = workers × pages-per-issue.")
    p.add_argument("--api-workers",    type=int, default=3,
                   help="Parallel issues for Claude API calls (default: 3)")
    p.add_argument("--serial",         action="store_true",
                   help="Disable API parallelism — process one issue at a time")
    p.add_argument("--tier",           default="default",
                   choices=["default", "build", "custom"],
                   help="Anthropic rate limit tier: default=50rpm, build=1000rpm")

    args = p.parse_args()

    if args.all:
        args.discover    = True
        args.download_ocr = True

    if not any([args.configure, args.discover, args.download_ocr, args.download_pdf,
                args.preload_images, args.correct, args.translate, args.status]):
        p.print_help()
        return

    # -----------------------------------------------------------------------
    # Global config (API key, model, tier — shared across collections)
    # -----------------------------------------------------------------------
    global_config = load_global_config()

    if args.configure:
        global_config = configure_global(global_config)

    # -----------------------------------------------------------------------
    # Locate / create collection config
    # -----------------------------------------------------------------------
    config_path = find_config_path(args.config_dir)

    if args.configure or not config_path.exists():
        if not args.configure and not config_path.exists():
            print("\nNo collection.json found. Starting configuration wizard...\n")
        config = run_configure(
            existing=json.loads(config_path.read_text()) if config_path.exists() else None
        )
        # Now we know the output dir name — set the real path
        output_dir = Path(config["output_dir_name"])
        config_path = output_dir / "collection.json"
        init_paths(output_dir)
        save_config(config, config_path)
        if args.configure:
            print("Configuration complete. Run --discover to begin downloading.")
            return
    else:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        output_dir = Path(config["output_dir_name"])
        if args.config_dir:
            output_dir = Path(args.config_dir)
        init_paths(output_dir)
        print(f"Collection : {config['title_name']}")
        print(f"Directory  : {output_dir}/\n")

    OUTPUT_DIR.mkdir(exist_ok=True)

    # -----------------------------------------------------------------------
    # Resolve API key: flag → env → config.json → collection.json (legacy)
    # -----------------------------------------------------------------------
    resolved_api_key = (
        args.api_key
        or os.environ.get("ANTHROPIC_API_KEY", "")
        or global_config.get("anthropic_api_key", "")
        or config.get("anthropic_api_key", "")
    )

    worker_args = [
        "--config-path", str(config_path),
    ]
    if args.resume:              worker_args.append("--resume")
    if args.ark:                 worker_args += ["--ark",                args.ark]
    if args.date_from:           worker_args += ["--date-from",          args.date_from]
    if args.date_to:             worker_args += ["--date-to",            args.date_to]
    if resolved_api_key:         worker_args += ["--api-key",            resolved_api_key]
    if args.issue_delay:         worker_args += ["--issue-delay",        str(args.issue_delay)]
    if args.api_workers:         worker_args += ["--api-workers",        str(args.api_workers)]
    if args.serial:              worker_args.append("--serial")
    if args.tier:                worker_args += ["--tier",               args.tier]
    if hasattr(args, 'max_output_tokens') and args.max_output_tokens != 32000:
        worker_args += ["--max-output-tokens", str(args.max_output_tokens)]

    # -----------------------------------------------------------------------
    # Execute steps
    # -----------------------------------------------------------------------
    if args.status:
        show_status(config)
        return

    if args.discover:
        discover_issues(config)

    if args.download_ocr:
        download_all_ocr(config, resume=not args.force_ocr, workers=args.workers)

    if args.download_pdf:
        download_all_pdfs(resume=args.resume)

    if args.preload_images:
        correct_preload_args = list(worker_args)
        if args.retry_failed:
            correct_preload_args.append("--retry-failed")
        correct_preload_args += ["--workers", str(args.workers)]
        run_worker("unt_ocr_correct.py", correct_preload_args + ["--preload-images"])

    if args.correct:
        correct_args = list(worker_args)
        if args.retry_failed:
            correct_args.append("--retry-failed")
        run_worker("unt_ocr_correct.py", correct_args)

    if args.translate:
        run_worker("unt_translate.py", worker_args)

    if args.render_pdf:
        pdf_args = [
            "--config-path", str(config_path),
            "--columns",     str(args.columns),
        ]
        if args.resume: pdf_args.append("--resume")
        if args.ark:    pdf_args += ["--ark",       args.ark]
        if args.date_from: pdf_args += ["--date-from", args.date_from]
        if args.date_to:   pdf_args += ["--date-to",   args.date_to]
        run_worker("unt_render_pdf.py", pdf_args)

    if not any([args.preload_images, args.correct, args.translate]):
        print("\nDone.")


if __name__ == "__main__":
    main()
