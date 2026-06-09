# Botric — Prompt Generation Update #7: Persona Coverage Fix (code-guaranteed spread)

**Build specification (standalone) — seventh deliverable.** A correctness fix to the persona→region
assignment shipped in Update #6. Same stack. **No schema change, no UI change.** Touches
`generators/post_gen.py` only.

---

## Why (Update #6 still failed)

Update #6 moved persona tagging into a dedicated `_assign_personas_to_regions` pass, but it **still
collapsed in production**: a 7-region cluster assigned a persona to only 2 regions — both the *same*
persona — and left the other 5 blank/"(broad)".

**Root cause:** Update #6 delegated *coverage* to the LLM. Its assignment prompt told the model
*"if no persona genuinely fits, return an empty list"* and *"be honest, don't stretch."* The real
model over-uses that escape hatch — it returns one persona for ~2 regions and `[]` for the rest. The
code then faithfully renders each `[]` as `"(broad)"`, producing "2 same + 5 blank." (Update #6's unit
tests passed only because they fed well-distributed *stub* data — they exercised the enforcement math,
never the LLM's actual sparse output.)

Secondary gap: in `create_cluster`, `_merge_observed` runs **after** the fan-out's assignment, so
manually-pasted observed-query regions never received a persona.

**Fix principle:** the LLM supplies only a *preference ranking*; **code guarantees both full coverage
and even distribution**. Coverage must never depend on the LLM returning good data.

---

## The fix (`generators/post_gen.py`)

### `_assign_personas_to_regions(self, rewrites, personas)` — rewritten
1. **Fit personas** = `fit ∈ {yes,maybe}`. **0** → every `persona=""`, return (no LLM call). **1** →
   assign it to every region (it's the only fit), return.
2. **One Claude call — full ranking, no empty escape.** For each region, rank **ALL** fit personas
   most- → least-likely-to-ask (by goal/trigger/vocab). The prompt explicitly requires a *complete*
   ranking per region and forbids omissions / empty lists. (We want the preference signal, not a fit
   gate.)
3. **Backfilled candidate lists (the robustness core).** For each region, candidates =
   `[valid LLM-ranked labels, de-duped]` **+ every fit persona not already listed** (original brand
   order). So **every region can reach every fit persona**, LLM preference first. Even if the LLM
   returns junk, empties, or an identical ranking for all regions, code still has a full fallback.
4. **Greedy assignment under a cap.**
   - `cap = max(1, ceil(len(regions) / len(fit_personas)))` — the minimum cap that still permits full
     coverage; using exactly it maximizes spread. (`R < N` ⇒ cap 1 ⇒ 1:1 best-matching.)
   - Walk regions in order; each takes its highest-ranked candidate with `count < cap`, increment.
     Because every region carries all personas and `cap × N ≥ regions`, **every region gets a real
     persona** — no blanks, no `"(broad)"`. (Defensive fallback: least-used persona; should never fire.)
5. Mutate `rewrite["persona"]`; log the region→persona map for diagnosability.

**Still fit-driven, not forced:** each region gets the *best-fitting available* persona; the cap only
redirects to the next-best fit when a persona is already even-handedly used — principled fairness over
a fit ranking, never an arbitrary filler and never an empty region.

### Cover every path
- The call stays in `_fanout_rewrites` after `_dedup_cap_regions` (covers `generate_posts` + base
  `create_cluster`).
- In `create_cluster`, after `_merge_observed` in **both** observed branches (reuse-with-observed and
  fresh-with-observed), call `_assign_personas_to_regions(rewrites, self._parse_personas(brands))` on
  the **full** rewrite set, so observed-added regions are assigned too and the whole cluster stays
  evenly distributed. (No extra call when no observed queries were supplied.)

---

## Verification (tests reproduce the FAILURE MODE, not happy-path stubs)
`_assign_personas_to_regions` with stubs that mimic the real LLM:
- **Production symptom** — LLM returns one persona for 2 regions + `[]` for 5 (of 7) → result
  `P1:2,P2:2,P3:2,P4:1`: **all 7 assigned, ≥4 distinct, none over cap (2), zero blanks/broad.**
- **Identical full ranking for all 7** → cap forces the same spread.
- **Garbage / non-dict response** → code backfill still covers all 7, capped, no blanks.
- **1 fit persona / 7 regions** → all 7 get it, **no LLM call**. **0 fit personas** → all `""`, no call.
- **Small cluster (R<N)** → cap 1, 1:1 best-matching.
- **Field safety** — `query` / `region` / `variants` untouched; only `persona` mutated.
- `python3 ast.parse` on `post_gen.py`; inline `<script>` `node --check` (no UI change, sanity only).

## Preserved invariants (unchanged)
- A region keeps **multiple seed references** via its `variants` array (same-region duplicates are
  folded in by `_dedup_cap_regions`).
- **Manually entered fan-out queries are kept** (`_merge_observed` → variant of the matching region or
  a new `source:"manual"` region; only exact-duplicate queries skipped).
- The persona pass mutates **only** `rewrite["persona"]`.

## Operational notes
- **Forward-only:** to apply to an existing skewed cluster (e.g. PeterMD longevity), **delete and
  recreate** it.
- If a cluster still looks thin, the brand's stored personas may be a stale 3–5 set with several
  `fit:"no"`. Use **Edit Brand → Regenerate personas** for the fresh 4–6 distinct set, then recreate
  the cluster. The fix guarantees spread across whatever fit personas exist.

## Files
`generators/post_gen.py`. No schema/UI change. Supersedes the assignment logic from Update #6.

---

*End of update spec.*
