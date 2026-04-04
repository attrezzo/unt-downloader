#!/usr/bin/env python3
"""
unt_cost_estimate.py — Model selection, pricing lookup, and cost confirmation.

Used by unt_ocr_correct.py and unt_translate.py before any Claude API calls.
Also called during --configure to set a default model in collection.json.

TOKEN ESTIMATES (calibrated from actual $177.28 run — 118 issues, 944 pages, Sonnet 4.6):

  OCR correction per page:
    Input:  ~57,000 tokens  (~56k image tokens + ~1k text/system prompt)
    Output: ~1,200 tokens
    → $0.187/page at Sonnet 4.6 rates

  Translation per page (text-only — no image sent):
    Input:  ~1,670 tokens  (system prompt ~880 + corrected OCR + header ~790)
    Output: ~1,500 tokens
    → ~$0.028/page at Sonnet 4.6 rates  (86% cheaper than correction)

  The image token count dominates. A full-res newspaper scan at UNT IIIF 'max'
  resolves to roughly 2400×3100 pixels, which Claude tiles into ~35 512×512
  tiles at ~1,600 tokens each ≈ 56,000 image tokens per page.

  The previous estimate used 1,500 input tokens (text only) and was 91% below
  actual cost. Always recalibrate if switching to smaller image sizes.
  Anthropic does not expose pricing via their API — it is only shown on their
  website (https://platform.claude.com/docs/en/about-claude/pricing) which
  is JavaScript-rendered and not scrapeable.

  This module uses a two-source approach:
    1. /v1/models API  → live list of models your API key can actually call
    2. pricing.json    → human-maintained price table, shipped alongside
                         this script and editable by the user

  When a model appears in the live API list but NOT in pricing.json, it is
  shown as "price unknown" and the user is directed to update pricing.json.

  To update prices: edit pricing.json in the same folder as this script.
  Verify current prices at: https://platform.claude.com/docs/en/about-claude/pricing
"""

import sys
import os
import json
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

PRICING_FILE = Path(__file__).parent / "pricing.json"
PRICING_URL  = "https://platform.claude.com/docs/en/about-claude/pricing"

# Tier display order — models are sorted by tier priority then name
TIER_ORDER = {"haiku": 0, "sonnet": 1, "opus": 2, "unknown": 3}


# ---------------------------------------------------------------------------
# Load pricing.json
# ---------------------------------------------------------------------------
def load_pricing() -> dict:
    """
    Load the local pricing table.
    Returns dict of {model_id: {input, output, tier, note}} or {}.
    """
    if not PRICING_FILE.exists():
        return {}
    try:
        data = json.loads(PRICING_FILE.read_text(encoding="utf-8"))
        return data.get("models", {})
    except Exception:
        return {}


def pricing_meta() -> dict:
    """Return the _meta block from pricing.json."""
    if not PRICING_FILE.exists():
        return {}
    try:
        return json.loads(PRICING_FILE.read_text(encoding="utf-8")).get("_meta", {})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Fetch live model list from Anthropic API
# ---------------------------------------------------------------------------
def fetch_available_models(api_key: str) -> list[dict]:
    """
    Call GET /v1/models and return list of model dicts.
    Each dict has at least: id, display_name (may be absent for older API versions).
    Returns [] on any failure.
    """
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
            },
            method="GET"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data.get("data", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Build the model menu
# ---------------------------------------------------------------------------
def build_model_menu(api_key: str) -> list[dict]:
    """
    Combine live model list with pricing data to produce a menu.

    Each entry:
      {
        id:           "claude-sonnet-4-6",
        display_name: "Claude Sonnet 4.6",   # from API or derived
        input:        3.00,                  # $/MTok or None
        output:       15.00,                 # $/MTok or None
        tier:         "sonnet",
        note:         "...",
        priced:       True/False,
      }

    Entries are sorted haiku < sonnet < opus, then alphabetically.
    Aliases (same price as another model) are omitted to reduce clutter.
    """
    pricing  = load_pricing()
    live     = fetch_available_models(api_key)
    live_ids = {m["id"] for m in live}

    menu     = []
    seen_ids = set()

    # Start with live models that have pricing
    for m in live:
        mid  = m["id"]
        if mid in seen_ids:
            continue
        seen_ids.add(mid)

        p = pricing.get(mid, {})
        entry = {
            "id":           mid,
            "display_name": m.get("display_name") or _derive_display_name(mid),
            "input":        p.get("input"),
            "output":       p.get("output"),
            "tier":         p.get("tier", _derive_tier(mid)),
            "note":         p.get("note", ""),
            "priced":       "input" in p,
        }
        menu.append(entry)

    # If the API call failed, fall back to everything in pricing.json
    if not live:
        for mid, p in pricing.items():
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            entry = {
                "id":           mid,
                "display_name": _derive_display_name(mid),
                "input":        p.get("input"),
                "output":       p.get("output"),
                "tier":         p.get("tier", _derive_tier(mid)),
                "note":         p.get("note", ""),
                "priced":       True,
            }
            menu.append(entry)

    # Sort: tier order first, then model id alphabetically
    menu.sort(key=lambda x: (TIER_ORDER.get(x["tier"], 3), x["id"]))

    # Remove pure aliases (same id prefix, same price as a more-specific entry)
    # e.g. "claude-haiku-4-5" when "claude-haiku-4-5-20251001" is already present
    deduped = []
    seen_prices = {}
    for entry in menu:
        key = (entry["tier"], entry.get("input"), entry.get("output"))
        prev_id = seen_prices.get(key, "")
        # If this id is a prefix/alias of an already-included id, skip it
        if prev_id and (entry["id"].startswith(prev_id) or prev_id.startswith(entry["id"])):
            continue
        seen_prices[key] = entry["id"]
        deduped.append(entry)

    return deduped


def _derive_display_name(model_id: str) -> str:
    """Derive a human-readable name from a model ID."""
    # claude-sonnet-4-6 → Claude Sonnet 4.6
    parts = model_id.replace("claude-", "").split("-")
    # drop date suffixes like 20251001
    parts = [p for p in parts if not p.isdigit() or len(p) < 6]
    return "Claude " + " ".join(p.capitalize() for p in parts)


def _derive_tier(model_id: str) -> str:
    mid = model_id.lower()
    if "haiku"  in mid: return "haiku"
    if "sonnet" in mid: return "sonnet"
    if "opus"   in mid: return "opus"
    return "unknown"


# ---------------------------------------------------------------------------
# Main entry point: model selection + cost estimate + confirmation
# ---------------------------------------------------------------------------
def choose_model_and_confirm(
    api_key:             str,
    pages_to_process:    int,
    step_name:           str,
    input_tok_per_page:  int,
    output_tok_per_page: int,
    default_model:       str = "claude-sonnet-4-6",
) -> str:
    """
    Interactive model selector + cost estimate + y/N confirmation.

    Shows live model list with pricing and estimated cost for this run.
    Returns chosen model ID. Calls sys.exit(0) if user declines.

    Args:
        api_key:             Anthropic API key (used to fetch live model list)
        pages_to_process:    number of pages that will be sent to the API
        step_name:           "OCR correction" or "Translation"
        input_tok_per_page:  estimated input tokens per page
        output_tok_per_page: estimated output tokens per page
        default_model:       pre-selected model (from collection.json)
    """
    if pages_to_process == 0:
        print("Nothing to process — all files already complete.")
        sys.exit(0)

    print(f"Fetching available models from Anthropic API ...")
    menu = build_model_menu(api_key)

    if not menu:
        print("  ⚠ Could not retrieve model list. Check your API key and network.")
        sys.exit(1)

    meta = pricing_meta()
    live_ok = bool(fetch_available_models.__code__)   # always True; just for display

    total_in  = pages_to_process * input_tok_per_page
    total_out = pages_to_process * output_tok_per_page

    # ── Print header ──────────────────────────────────────────────────────
    print()
    print("─" * 72)
    print(f"  {step_name.upper()} — MODEL SELECTION & COST ESTIMATE")
    print(f"  Pages to process   : {pages_to_process:,}")
    print(f"  Est. input tokens  : {total_in:,}  ({input_tok_per_page:,}/page)")
    print(f"  Est. output tokens : {total_out:,}  ({output_tok_per_page:,}/page)")
    if meta.get("last_verified"):
        print(f"  Pricing verified   : {meta['last_verified']}  "
              f"(edit pricing.json to update)")
    print("─" * 72)

    # ── Print model table ─────────────────────────────────────────────────
    default_idx = 1
    print(f"  {'#':<3}  {'Model ID':<36}  {'In$/MTok':>8}  {'Out$/MTok':>9}  "
          f"{'Est. cost':>16}  Note")
    print(f"  {'─'*3}  {'─'*36}  {'─'*8}  {'─'*9}  {'─'*16}  {'─'*20}")

    for i, entry in enumerate(menu, start=1):
        mid    = entry["id"]
        is_def = (mid == default_model or
                  (default_model and mid.startswith(default_model)) or
                  (default_model and default_model.startswith(mid)))
        if is_def:
            default_idx = i

        if entry["priced"] and entry["input"] is not None:
            cost     = (total_in * entry["input"] + total_out * entry["output"]) / 1_000_000
            cost_hi  = cost * 1.4
            cost_str = f"~${cost:.2f}–${cost_hi:.2f}"
            in_str   = f"${entry['input']:.2f}"
            out_str  = f"${entry['output']:.2f}"
        else:
            cost_str = "price unknown"
            in_str   = "  ?"
            out_str  = "   ?"

        marker = " ◄" if is_def else "  "
        note   = entry.get("note", "")[:28]
        print(f"  {i:<3}  {mid:<36}  {in_str:>8}  {out_str:>9}  "
              f"{cost_str:>16}{marker}  {note}")

    # ── Unpriced models note ──────────────────────────────────────────────
    unpriced = [e for e in menu if not e["priced"]]
    if unpriced:
        print()
        print(f"  ⚠ {len(unpriced)} model(s) have no price in pricing.json.")
        print(f"    Verify at: {PRICING_URL}")
        print(f"    Then edit: pricing.json")

    print("─" * 72)
    print()

    # ── Model selection ───────────────────────────────────────────────────
    while True:
        raw = input(f"Select model [1–{len(menu)}, default={default_idx}]: ").strip()
        if raw == "":
            chosen_idx = default_idx
            break
        try:
            chosen_idx = int(raw)
            if 1 <= chosen_idx <= len(menu):
                break
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(menu)}.")

    chosen = menu[chosen_idx - 1]
    mid    = chosen["id"]

    if chosen["priced"] and chosen["input"] is not None:
        cost    = (total_in * chosen["input"] + total_out * chosen["output"]) / 1_000_000
        cost_hi = cost * 1.4
        cost_line = f"~${cost:.2f} – ${cost_hi:.2f}"
    else:
        cost_line = "unknown (no price in pricing.json)"

    print()
    print(f"  Selected  : {mid}")
    print(f"  Est. cost : {cost_line}")
    print()

    confirm = input(f"Proceed with {step_name}? [y/N]: ").strip().lower()
    if confirm not in ("y", "yes"):
        print("Cancelled.")
        sys.exit(0)

    print()
    return mid


# ---------------------------------------------------------------------------
# Lightweight version for --configure: pick a default model and store it
# ---------------------------------------------------------------------------
def configure_model(api_key: str, current_model: str = "claude-sonnet-4-6") -> str:
    """
    Called during --configure to let the user set a default model.
    Returns chosen model ID.
    Does NOT show page counts or cost estimates — just the model menu.
    """
    print("Fetching available Claude models ...")
    menu = build_model_menu(api_key)

    if not menu:
        print("  ⚠ Could not retrieve model list — API key may be invalid or network unavailable.")
        print(f"  Keeping current default: {current_model}")
        return current_model

    meta = pricing_meta()
    print()
    print("─" * 72)
    print("  AVAILABLE CLAUDE MODELS")
    if meta.get("last_verified"):
        print(f"  Prices from pricing.json (last verified {meta['last_verified']})")
        print(f"  Verify at: {PRICING_URL}")
    print("─" * 72)
    print(f"  {'#':<3}  {'Model ID':<36}  {'Input $/MTok':>12}  {'Output $/MTok':>13}  Note")
    print(f"  {'─'*3}  {'─'*36}  {'─'*12}  {'─'*13}  {'─'*24}")

    default_idx = 1
    for i, entry in enumerate(menu, start=1):
        is_def = (entry["id"] == current_model or
                  current_model.startswith(entry["id"]) or
                  entry["id"].startswith(current_model))
        if is_def:
            default_idx = i

        if entry["priced"] and entry["input"] is not None:
            in_str  = f"${entry['input']:.2f}"
            out_str = f"${entry['output']:.2f}"
        else:
            in_str  = "?"
            out_str = "?"

        marker = " ◄ current" if is_def else ""
        note   = entry.get("note", "")[:24]
        print(f"  {i:<3}  {entry['id']:<36}  {in_str:>12}  {out_str:>13}  {note}{marker}")

    print("─" * 72)
    print()

    while True:
        raw = input(f"Select default model [1–{len(menu)}, default={default_idx}]: ").strip()
        if raw == "":
            chosen_idx = default_idx
            break
        try:
            chosen_idx = int(raw)
            if 1 <= chosen_idx <= len(menu):
                break
        except ValueError:
            pass
        print(f"  Enter a number between 1 and {len(menu)}.")

    chosen = menu[chosen_idx - 1]["id"]
    print(f"  Default model set to: {chosen}")
    return chosen
