# Botric — Prompt Generation Update #10: Region Identity by Construction + Title Diversity

**Build specification (standalone) — tenth deliverable.** Two fixes. Same stack. **No schema change**
(the new region id rides in the existing `ai_search_meta` JSON). Touches `generators/post_gen.py`,
`db.py`, `app.py`, `templates/index.html`.

---

# Part A — Bind a post to its rewrite BY CONSTRUCTION (stop re-deriving region from text)

## Symptom
On a cluster for a supplier brand ("concrete tools direct", anchor "US"), generating for a selected
region produced a post that landed on the **wrong region or none** — even with a **single** region
selected; manual-add by post # landed on a **different** region; repeats were inconsistent.

## Root cause
The post→region link was re-derived from text. Generation was a **batch** (the model wrote N titles
for N rewrites in one call and self-reported, in free text, which title was for which rewrite), and the
system then **fuzzy-matched that paraphrase back to a region**. For a supplier brand every region shares
the dominant tokens ("concrete tools"); the distinguishing words are a tiny minority, so the match
lands on a sibling region or below threshold (→ no region). The single-region case failed for the same
reason: the saved label was the model's paraphrase, re-matched against look-alike regions. Manual-add
ignored the region entirely and LLM-classified the title.

**Principle:** a post is *produced for* a specific rewrite, so its region must be **recorded at
production time**, never re-inferred.

## Changes
1. **Per-rewrite generation** (`post_gen.py`, in `generate_posts`): when in cluster gap/target mode
   (`run_gaps is not None`), replace the per-intent batch with a per-rewrite loop. For each targeted
   rewrite, call `_generate_candidates_for_intent(..., coverage_focus={...,"rewrites":[gap_q]}, count≈2)`
   showing the model **only that one rewrite**, run the existing scorer + `_embedding_gate` +
   `_select_cluster_best(…, 1)`, and **stamp `post["target_query"] = gap_q` and `post["region"]` from
   the rewrite** — never the model's self-report, never a fuzzy match. No candidate clears the bar →
   skip (gap stays open, logged), never mis-bind. Non-cluster modes keep the batch loop.
2. **Stable region identity on the post** (`post_gen.py` save block; `db.py`
   `attach_posts_to_cluster`): write `region` into `ai_search_meta` (alongside seed/anchor/
   target_query/persona). `get_ai_search_posts_for_seed` now surfaces it.
3. **Coverage joins on identity** (`app.py` `/api/live-posts/clusters`): for each post, credit the
   rewrite whose **region label == `ai_search_meta.region`** (exact, unique within a cluster) → else
   exact canonical query → else the tolerant matcher **only as a legacy fallback** for pre-stamp posts.
4. **Manual add binds the ticked region** (`templates/index.html`, `app.py`): each rewrite checkbox
   carries `data-region`; if exactly one is ticked, `lsubsClusterAddPosts` sends `region` and
   `api_cluster_add_posts` builds `region_by_num = {num: region}` directly, **skipping
   `_classify_regions`**. Zero/multiple ticked → old auto-classify.

---

# Part B — Break the "best… best… best…" title monoculture (NO example templates)

## Symptom
Most generated titles came out as "best X for Y."

## Root cause
"best X for Y" was handed to the model as a **copyable example** in three places, and any example
becomes a template the model defaults to:
1. fan-out region axis #1 mapped `Category / best tool → "best X for Y"`;
2. candidate-gen rule #4: *"Prioritize titles … (e.g., 'best X for Y', 'which X should I use')."*;
3. the relevance scorer's 8–10 band led with "best X for Y", so the selector **up-ranked** "best…"
   titles and filtered out the varied ones.
(plus a body-weave example "…the best [category] for…").

## Fix — remove the example surface forms, describe the PROPERTY, add variety
- **Fan-out axes:** dropped the quoted `→ "best X for Y"` template; the category axis is now described
  by intent ("the leading option in the category for their need — phrase naturally, don't default to a
  'best …' wording"), and step 3 adds: vary phrasing across regions, don't force a "best …" shape.
- **Candidate rule #4:** no examples — *"each title is a genuine recommendation question (its natural
  answer names a product/service); phrase it as a real person would and VARY the structure/opening
  across the batch; do not default to a 'best …' shape."*
- **Scorer:** judges by **the answer the title would get, not its wording** — any phrasing scores high
  if the natural answer is a specific recommendation; explicitly does **not** favor "best …". So
  variety survives selection.
- **Body-weave example:** replaced the "best [category] …" example with a property instruction.

"best" stays *allowed* (a real person sometimes asks it) — it's just no longer manufactured or rewarded.
Combined with Part A's per-region generation, posts now mirror the varied region wording.

---

## Verification
- **Coverage identity-join (simulation):** a same-anchor cluster (`["where to buy concrete tools
  online", "best place to order concrete tools for pros", "bulk concrete tool supplier with fast
  shipping"]`) with **paraphrased** `target_query` on each post → every post credited to its **stamped
  region** (identity beats text); a legacy post with no region stamp still falls back to the fuzzy
  matcher. All correct, none cross-bound, none dropped.
- **Part B:** `grep` confirms the `"best X for Y"` template string is gone from all prompts; the rule
  and scorer text is property-based.
- `python3 ast.parse` on db/app/post_gen; inline `<script>` `node --check`.

## Operational note
Posts generated before this update have no region stamp and may sit on the wrong region. Re-run
**Generate for selected** per region (or delete + regenerate) to rebind; new posts are correct by
construction.

## Files
`generators/post_gen.py`, `db.py`, `app.py`, `templates/index.html`. No schema change.

---

*End of update spec.*
