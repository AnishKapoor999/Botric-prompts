# Botric — Prompt Generation Update #6: Fit-Driven Persona→Region Assignment

**Build specification (standalone) — sixth deliverable.** Replaces the in-prompt persona tagging
from Update #5 with a dedicated, code-enforced assignment step. Same stack. **No schema change, no
UI change.** Touches `generators/post_gen.py` and `generators/brand_enrichment.py` only.

---

## Why

Update #5 tried to fix uniform persona tags by strengthening the *prompt* instruction ("represent
multiple personas, never tag every region with the same one"). **It didn't hold.** A real 7-region
longevity cluster came back with personas on only 4 regions, 3 of them the *same* persona, and 3
blank. Asking the LLM to distribute personas inline, across a JSON array, while it's also doing the
fan-out, is unreliable — it anchors on the first persona and leaves the rest unset.

**Fix:** stop asking the fan-out to tag personas at all. Make persona→region assignment a **separate,
deterministic pass** where the LLM only *judges fit per region* and **code** enforces the
distribution constraints (cap + full coverage). Assignment stays **fit-driven — never forced**: a
region only ever gets a persona that genuinely fits it; when no fitting persona is available it
becomes an honest `"(broad)"`, not an arbitrary filler.

---

## 1. New assignment pass (`generators/post_gen.py`)

### `_assign_personas_to_regions(self, rewrites, personas)` (new — owns persona tagging)

Mutates each `rewrite["persona"]` in place. Steps:

1. **Fit filter.** Keep personas with `fit ∈ {"yes","maybe"}` (the `"no"` personas are the
   winnability filter — excluded). If there are **no** fit personas (or no regions), set every
   `rewrite["persona"] = ""` and **return without calling the LLM** (no tagging, no regression for
   brands without good personas).

2. **One Claude call — judge fit, ranked.** Give the LLM the fit personas (label + profile + goal +
   trigger + vocab) and the list of regions (their queries). For **each** region it returns the
   personas that *genuinely* fit — i.e. a real person of that type would plausibly type that exact
   question — **ranked best-fit first**, by goal/trigger/vocab. Empty list when none genuinely fit
   (a broad question almost anyone asks). The prompt explicitly says *be honest, don't stretch a
   persona to fit*. Returns `{"assignments": [["label", ...], ...]}` aligned to region order.

   Normalize the returned labels: lowercase-match against the real persona labels, drop anything that
   isn't a valid fit persona, de-dup, preserve rank order. So a hallucinated/garbled label can never
   be assigned.

3. **Deterministic enforcement (the reliability + fairness layer):**
   - `cap = max(2, ceil(len(regions) / len(fit_personas)))` — the most times any one persona may be
     used. The `max(2, …)` floor keeps small clusters from being over-restricted.
   - Process regions **most-constrained first** (fewest fitting candidates first). This matters: a
     region whose *only* fit is persona P must claim P before less-constrained regions use P up.
   - For each region, walk its ranked candidates and assign the **first** one whose use-count is
     `< cap` (increment that persona's count). If all its fitting candidates are at the cap — or it
     had no fitting candidate — assign `"(broad)"`.

   **Guarantees:** no region left blank (always a specific persona or `"(broad)"`); no persona used
   more than `cap` times; every assignment comes from a *genuine-fit* candidate — **never** a
   non-fitting persona forced in to fill a slot.

### Wiring in `_fanout_rewrites`
After `rewrites = self._dedup_cap_regions(rewrites)`, call
`self._assign_personas_to_regions(rewrites, self._parse_personas(brands))`. (It re-filters to fit
personas internally, so passing all is fine.) **Remove the inline persona-tagging instruction** from
the fan-out prompt and the `persona` field from the fan-out JSON spec — the fan-out no longer tags
personas. **Keep the persona LENS** (the block that tells the model which kinds of asker exist so it
shapes *which questions* to create and grounds phrasing); only the *tagging* responsibility moves out.

Both `create_cluster` (fan-out only) and `generate_posts` inherit this automatically, since both run
through `_fanout_rewrites`.

---

## 2. More personas to fall back on (`generators/brand_enrichment.py`)

In `generate_brand_personas`, raise the target from **3–5 → 4–6 DISTINCT** personas (keep the
"distinct / non-overlapping, no fabrication, honest fit" rules). Bump `max_tokens` to 1400 and return
up to 6. Rationale: with the cap in place, more genuine personas means a capped region has more
real fallbacks before it has to settle for `"(broad)"`.

---

## Verification

Unit-test `_assign_personas_to_regions` with a stubbed ranking call:

- **The headline case** — 7 regions, 5 personas, LLM ranks the *same* persona top for all of them →
  result is that persona used exactly `cap` (=2) times and the remaining 5 regions `"(broad)"`. No
  blanks, cap respected, **no forced fillers**.
- **Next-best fallback** — regions whose only fit is P (most-constrained) claim P up to the cap; a
  less-constrained region that also lists Q falls to **Q** (its next-best *fit*), not to `"(broad)"`
  and not to a non-listed persona.
- **Cap-exhausted → broad** — a region whose only fitting persona is already at the cap, with no
  other fit, becomes `"(broad)"` (never a non-fitting persona).
- **Empty-candidate region → `"(broad)"`.**
- **No fit personas → every region `""`**, and the LLM is **not** called.
- **Robustness** — empty rewrites no-op; case-insensitive label matching; bogus/hallucinated labels
  dropped.

Plus: `python3 ast.parse` on `post_gen.py` / `brand_enrichment.py`. No UI change (run inline
`<script>` `node --check` anyway to confirm nothing regressed).

## Notes

- **One extra Claude call per fan-out** (the assignment pass) — one-time per cluster build.
- `"(broad)"` is reserved for genuinely cross-cutting regions or cap-exhausted fits — **not** a
  catch-all filler.
- **Forward-only:** existing clusters keep their old persona tags in `rewrites_json`. To apply the
  fix to a skewed cluster (e.g. the PeterMD longevity one), **delete and recreate** it.
- Reuses `_parse_personas`, `_fit_personas`, `_dedup_cap_regions`, `_fanout_rewrites`,
  `normalize_rewrites`. Backward compatible; no schema/UI change.
- Files: `generators/post_gen.py`, `generators/brand_enrichment.py`.

---

*End of update spec.*
