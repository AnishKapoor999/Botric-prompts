# Botric — Prompt Generation Update #9: Snap to the Produced Region (scope fix)

**Build specification (standalone) — ninth deliverable.** A one-line-scope correctness fix to the
target_query snap added in Update #8. Same stack. **No schema/UI change.** Touches
`generators/post_gen.py` only.

---

## Why

Update #8 added a generate-time "snap" that rewrites each candidate's (paraphrased) `target_query` to
the canonical cluster rewrite it best matches, so coverage keys off the exact rewrite. But it matched
against **`coverage_focus["rewrites"]` — the ENTIRE cluster.** A post produced for gap A could then
snap onto a textually-similar **sibling** rewrite B elsewhere in the cluster, so it filled the **wrong
region** ("it assigned the prompts but not to the current region from where they were produced").

The generation prompt already steers each batch to only the **current gaps** for that intent
(`intent_focus["rewrites"]`, i.e. the shrinking `run_gaps`) and instructs the model to copy the
assigned rewrite into `target_query`. So the snap only needs to resolve a paraphrase **within those
targeted gaps** — never the whole cluster.

---

## The fix (`generators/post_gen.py`, in `generate_posts`)

In the per-intent loop, the snap now matches against **`intent_focus["rewrites"]`** (the gaps this
batch was told to cover) instead of `coverage_focus["rewrites"]` (all rewrites):

```python
if ai_search and intent_focus and intent_focus.get("rewrites"):
    _canon = intent_focus["rewrites"]          # the gaps THIS batch targeted
    for c in candidates:
        _m = self.db.match_query_to_rewrites(c.get("target_query"), _canon)
        if _m:
            c["target_query"] = _m
```

`intent_focus` is the gap-restricted view already built each iteration
(`{**coverage_focus, "rewrites": run_gaps}` in gap-fill mode; equal to `coverage_focus` for a plain
no-seed fan-out). Scoping the snap to it keeps every post bound to the region it was actually generated
from, while still converting the model's paraphrase into the exact rewrite string that the
`run_gaps` shrink, `last_coverage`, and the Clusters coverage view all match on.

The Clusters route (Update #8) is unchanged: with the saved `target_query` now equal to the correct
canonical rewrite, its **normalized-exact** match binds the post to the right region first (the fuzzy
fallback only ever applies to legacy paraphrased posts).

---

## Verification
- Scope check: for a cluster `[clinic A, treatments B (shares tokens), NAD+ C]` where a batch targeted
  only gap A, a paraphrased post snaps to **A** under the targeted-gap scope — no drift to sibling B.
- `python3 ast.parse` on `generators/post_gen.py`. (No UI/schema change.)

## Notes
- Forward-only for the binding: posts generated under Update #8's all-cluster snap keep whatever
  region they were bound to. Re-generating those gaps now binds correctly; or an operator can re-run
  the gap fill.
- Files: `generators/post_gen.py`. Supersedes the snap scope from Update #8 (the matcher + Clusters
  route from #8 are unchanged).

---

*End of update spec.*
