# Botric — Prompt Generation Update #8: Cluster Gap-Fill Coverage Fix

**Build specification (standalone) — eighth deliverable.** Two bugs in the AI-Search Clusters gap-fill
flow. Same stack. **No schema change.** Touches `db.py`, `app.py`, `generators/post_gen.py`,
`templates/index.html`.

When an operator picks a gap (a fanned-out rewrite) in the Clusters tab and generates, the post was
created **but** (a) the top background-task loader never stopped, and (b) the gap kept showing as
uncovered (though the post appeared in the Posts tab).

---

## Bug 1 — background-task loader never finishes

**Symptom:** the top task-bar pill spins forever after a cluster gap-generate, even though the post
finished generating.

**Root cause:** the two cluster generate handlers (`lsubsClusterGenSelected`, `lsubsGenClusterGaps`)
called `_addBgTask(tid)` (which only *adds* the pill) and then ran their own `setInterval` poll that
reloaded the cluster on completion **but never called `_removeBgTask(tid)`**. They also had no 404
branch, so a task the server garbage-collected would spin forever too.

**Fix (`templates/index.html`):** both handlers now delegate to the shared `_pollTask(tid, label,
onComplete)` helper, which already removes the pill on completion, handles 404 (task GC'd) and error
states, shows a persistent toast, and runs `onComplete`. Pass `() => lsubsLoadClusters(true)` as
`onComplete` so the cluster refreshes (and the now-covered gaps update) when generation finishes.

---

## Bug 2 — generated post doesn't fill the gap

**Symptom:** after generating for a gap, the post shows in Posts but the rewrite still reads as a gap
(uncovered) in the Clusters view.

**Root cause:** a post stores `ai_search_meta.target_query` = the **model's self-reported** value (the
generation prompt asks it to "report which rewrite this title targets"), and the model *paraphrases*
it. The Clusters coverage view matched a rewrite to its posts by **exact** lowercased string equality
(`target_query == rewrite.query`). A paraphrase never equals the canonical rewrite, so coverage missed
— while the post still persisted (hence it appears in Posts but not against the gap).

**Fix — tolerant matching, in three coordinated places:**

1. **`db.py` — new matcher** `Database.match_query_to_rewrites(target_query, rewrite_queries)`
   (+ helpers `_norm_query`, `_content_tokens`, and a small `_MATCH_STOPWORDS` set):
   - normalized exact match first (lowercase, punctuation→space, collapse whitespace);
   - else compare **content tokens** (stopwords stripped), require **≥ 2 shared content tokens** (so a
     single common word like "longevity" can't fabricate coverage), score `max(Jaccard,
     overlap-coefficient)`, and return the best rewrite scoring **≥ 0.5** (else `None`). Tolerant of
     paraphrase, conservative against mis-binding.

2. **`app.py` — `/api/live-posts/clusters` coverage loop:** instead of an exact dict lookup, map each
   post to its best canonical rewrite via `db.match_query_to_rewrites(...)`; a rewrite is covered when
   ≥1 post maps to it. **This retroactively fixes already-generated posts** whose stored `target_query`
   is a paraphrase — they now bind to their rewrite on the next refresh (no regeneration needed).

3. **`generators/post_gen.py` — generate-time snap:** right after candidates are generated for an
   intent (AI-Search mode), snap each candidate's `target_query` to the canonical cluster rewrite via
   the same matcher (over `coverage_focus["rewrites"]`). This makes the **saved** `ai_search_meta`, the
   in-run `run_gaps` shrink, and the `last_coverage` summary all key off the exact rewrite string going
   forward — not a paraphrase.

---

## Verification
- **Matcher unit tests** (`Database.match_query_to_rewrites`): normalized exact (case/punctuation);
  paraphrase with an extra word; "versus"/"vs" reword; a **heavy reword** ("how do I reverse hormone
  decline as a 45yo man" → "how to reverse hormone decline in men over 40") binds; unrelated query →
  `None`; single shared content token → `None` (no false coverage); empty/`None`/empty-list guards;
  no cross-region mis-bind (a clinic query stays on the clinic rewrite).
- **Coverage-route simulation:** two posts with paraphrased `target_query` for two distinct gaps →
  both gaps flip to covered (2/3).
- `python3 ast.parse` on `db.py` / `app.py` / `generators/post_gen.py`; inline `<script>`
  `node --check`.

## Notes
- The matcher is intentionally conservative (≥2 shared content tokens, score ≥ 0.5) to avoid a post
  silently covering an unrelated rewrite. An unusually heavy reword that scores under 0.5 won't
  retro-bind; regenerating that one rewrite binds cleanly via the generate-time snap.
- No data migration: existing posts are re-matched live by the coverage route.
- Files: `db.py`, `app.py`, `generators/post_gen.py`, `templates/index.html`.

---

*End of update spec.*
