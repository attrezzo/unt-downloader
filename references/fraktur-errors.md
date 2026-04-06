# Fraktur OCR Error Patterns

Systematic failure modes when OCR software (ABBYY, Tesseract, etc.) reads 19th-century Fraktur script. Use this table to decode garbled ABBYY output back to the intended text.

## Tier 1 — Universal Swaps (appear on nearly every line)

These are caused by the fundamental similarity of Fraktur letterforms. Apply these corrections automatically when context supports them.

| OCR reads | Likely intended | Fraktur reason | Frequency |
|-----------|----------------|----------------|-----------|
| b | d | Fraktur b/d differ only in a tiny stroke direction | Very high |
| d | b | Reverse of above | Very high |
| f | s (long-s, ſ) | The long-s ſ is nearly identical to f in Fraktur | Very high |
| ſ | s | Long-s rendered as unknown character | High |
| unb | und | long-s + d → f + b | Very high |
| bie | die | b→d | Very high |
| bas | das | b→d | Very high |
| ber | der | b→d | Very high |
| ben | den | b→d | Very high |
| bem | dem | b→d | Very high |
| wurbe | wurde | b→d | Very high |
| durc | durch | long-s issues + ch ligature break | Very high |
| fic / fid) | sich | f→ſ→s, broken ligature | Very high |
| nid)t | nicht | broken ch ligature | High |
| nod) | noch | broken ch ligature | High |
| fein | sein | f→ſ→s | High |
| feine | seine | f→ſ→s | High |
| fid) | sich | f→ſ→s + broken ch | High |
| fommen | kommen | f→k confusion at word start | Moderate |
| fommen | sommen/kommen | context-dependent | Moderate |

## Tier 2 — Capital Letter Confusions

Fraktur capitals are ornate and highly similar to each other.

| OCR reads | Likely intended | Notes |
|-----------|----------------|-------|
| K | E | Fraktur K and E are very similar |
| E | K | Reverse |
| R | N | Fraktur R/N confusion |
| N | R | Reverse |
| V | B | Fraktur V/B confusion |
| B | V | Reverse |
| T | D | Fraktur T/D partial confusion |
| $ | H | ABBYY commonly reads Fraktur capital H as $ |
| M | W | Fraktur M/W confusion |
| W | M | Reverse |
| G | C | Fraktur G/C confusion |
| (S | C | ABBYY reads Fraktur C as (S or similar |
| % | N or W | Symbol substitution for unrecognized capitals |
| 3 | Z | Fraktur Z resembles 3 |
| 6 | G | Fraktur G resembles 6 in some hands |

### Common Capital Corrections in Context
| OCR garbage | Correct reading | Example context |
|------------|-----------------|-----------------|
| Kin | Ein | "Kin Antrag" → "Ein Antrag" |
| Kr | Er | "Kr sagt" → "Er sagt" |
| Km | Ein | "Km Artikel" → "Ein Artikel" |
| Tie | Die | "Tie Nachricht" → "Die Nachricht" |
| Ter | Der | "Ter Verlust" → "Der Verlust" |
| Tas | Das | "Tas Haus" → "Das Haus" |
| $ouston | Houston | Fraktur H → $ |
| $aus | Haus | Fraktur H → $ |
| $intertbür | Hinterthür | Fraktur H → $ |
| Roth | Noth | R→N, "Roth in der Provinz" → "Noth" |
| Rachricht | Nachricht | R→N |
| Renschenleben | Menschenleben | R→M |
| (Sounties | Counties | Fraktur C → (S |

## Tier 3 — Ligature and Combination Breaks

Fraktur uses many ligatures (connected letter pairs). OCR often breaks these into separate characters or misreads them entirely.

| OCR reads | Likely intended | Notes |
|-----------|----------------|-------|
| d) | ch | The ch ligature breaks apart |
| id) | ich | ch ligature with preceding i |
| ad) | ach | ch ligature with preceding a |
| od) | och | ch ligature with preceding o |
| ud) | uch | ch ligature with preceding u |
| ck | ct or ck | Usually ck is correct |
| fi | si | long-s + i |
| fl | sl or fl | Context-dependent |
| ft | st | long-s + t, very common |
| sz | ß | The ß in Fraktur can OCR as sz |
| dasz | daß | Common period spelling |
| ij | ü or y | Fraktur ij/ü confusion |
| ii | ü or u. | Fraktur ii can be ü, or "u." (abbreviation) |

## Tier 4 — Number/Letter Confusions

| OCR reads | Likely intended | Notes |
|-----------|----------------|-------|
| 1 | l or I | Fraktur l and numeral 1 |
| 0 | O | Zero vs capital O |
| 8 | B | In some Fraktur styles |
| 3 | Z | Fraktur Z looks like 3 |
| 9 | g | In damaged prints |
| 111 | Ill or III | Numeral 1 vs letter l |

## Tier 5 — Line-Break and Hyphenation Artifacts

German newspapers used extensive hyphenation. OCR often:
- Drops the hyphen entirely, joining word fragments
- Reads the = sign (used for hyphenation in Fraktur) as various characters
- Creates space-separated fragments of a single word

| OCR artifact | Likely intended |
|-------------|-----------------|
| word= (line end) | Hyphenated word continues on next line |
| word» | Same — » is OCR noise for = |
| word- | Standard hyphenation |
| wordanother | Two words joined when hyphen was lost |

## Application Notes

1. **Apply Tier 1 corrections aggressively** — they are almost always correct
2. **Apply Tier 2 with context checking** — verify the corrected word makes sense
3. **Apply Tiers 3-5 case by case** — these require more judgment
4. **Never correct a word that makes sense as-is** in Texas German dialect, even if it looks like an OCR error. See `texas-german.md` for dialect vocabulary.
5. **When multiple corrections are possible**, prefer the reading that fits the article's topic and 1890s German usage patterns
