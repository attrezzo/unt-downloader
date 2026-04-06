# Fraktur OCR Error Correction Table

Reference for correcting OCR errors from 19th-century German Fraktur typeface,
particularly as digitized from 35mm microfilm newspaper scans.

---

## Tier 1 — High-Frequency Swaps (apply automatically)

These are the most common OCR misreadings. The Fraktur letterforms for these
pairs are nearly identical, especially on degraded microfilm.

| OCR reads | Correct | Examples |
|-----------|---------|----------|
| b → d | context | ber→der, bie→die, bas→das, unb→und, bon→von, burch→durch, bann→dann, boch→doch, bies→dies, babei→dabei |
| d → b | context | daß→baß (rare), des→bes (context) |
| f → s (long-s) | context | fein→sein, fie→sie, fo→so, fich→sich, finb→sind, foll→soll, fehr→sehr, fchon→schon |
| s → f | rare | context-dependent |
| werben → werden | always | common verb confusion |
| wurbe → wurde | always | common verb confusion |
| finben → finden | always | common verb confusion |

**Rule:** In connected text, if a word starting with "f" makes no sense but
the same word with "s" is a common German word, prefer the "s" reading.
Same for b/d: check both possibilities against context.

---

## Tier 2 — Capital Letter Confusions

Capital Fraktur letters are ornate and frequently misread.

| OCR reads | Correct | Examples |
|-----------|---------|----------|
| $ → H | always for words | $ouston→Houston, $aus→Haus, $err→Herr, $ier→Hier |
| K → E | context | Ks→Es, Kine→Eine, Kr→Er |
| E → K | context | Eaiser→Kaiser, Einder→Kinder |
| R → N | context | Rach→Nach, Richt→Nicht |
| N → R | context | Necht→Recht |
| V → B | context | Vellville→Bellville, Vekanntmachung→Bekanntmachung |
| B → V | context | Berein→Verein, Bersammlung→Versammlung |
| G → E | context | Gr→Er, Gs→Es, Gin→Ein, Gine→Eine |
| W → M | rare | context-dependent |
| T → J | rare | context-dependent |

---

## Tier 3 — Ligature and Broken Character Repairs

Fraktur uses ligatures that often break in OCR.

| OCR reads | Correct | Notes |
|-----------|---------|-------|
| d) → ch | always | nid)t→nicht, fid)→sich, Bud)→Buch, Sad)e→Sache |
| cf → ck | always | zurücf→zurück, Glücf→Glück, Stücf→Stück, Drucf→Druck |
| « → ß | always | da«→daß, mu«→muß, gro«→groß, Stra«e→Straße |
| fi → si | context | fist→sist (rare, check) |
| fl → sl | context | rare |
| ff → ff | check | sometimes correct (Schiff, Affe) |
| st → ſt | display | keep as "st" in Latin transcription |
| tz → tz | usually correct | |

---

## Tier 4 — Missing/Extra Characters

Characters lost to ink bleed, foxing, or microfilm degradation.

| Pattern | Fix | Notes |
|---------|-----|-------|
| sic → sich | add h | extremely common |
| nac → nach | add h | extremely common |
| auc → auch | add h | extremely common |
| noc → noch | add h | extremely common |
| doc → doch | add h | extremely common |
| durc → durch | add h | |
| welc → welch | add h | |
| -h missing after c | add h | general pattern before consonants |

---

## Tier 5 — Line Break and Compound Word Repairs

German newspapers hyphenated freely at line ends.

| Pattern | Fix | Notes |
|---------|-----|-------|
| Wissen=\nschaft | Wissenschaft | = or - at line end → rejoin |
| Zeitungs-\nredakteur | Zeitungsredakteur | remove hyphen + newline |
| Bürger meister | Bürgermeister | space within compound |
| Ober Bürgermeister | keep as-is | sometimes a valid space |
| ver- handlung | Verhandlung | broken prefix |

**Rule:** If a word is split at a line break with = or -, try joining.
If the joined word is a valid German compound, use it.
If not, keep the break.

---

## Application Priority

When multiple corrections are possible:

1. Apply Tier 1 swaps first (highest confidence)
2. Apply Tier 2 capital corrections with context
3. Apply Tier 3 ligature repairs
4. Apply Tier 4 missing character fixes
5. Apply Tier 5 line break joins last

**Never apply a correction that produces a nonsense word.**
When in doubt, prefer the reading that produces valid German in context.
