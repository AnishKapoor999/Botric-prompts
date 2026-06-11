# Botric — Prompt Generation Update #11: Manual Fan-out Queries DEFINE the Regions

**Build specification (standalone) — eleventh deliverable.** Changes how manually-pasted fan-out
queries build a cluster. Same stack. **No schema change.** Touches `generators/post_gen.py`, `app.py`,
`templates/index.html`.

---

## Why
Pasted fan-out queries are *real, observed* sub-queries (captured from ChatGPT/Perplexity/Gemini), so
they should **define the cluster's regions** — not be folded in as supporting variants under
auto-generated regions. The old behavior (`_merge_observed`) classified each pasted query and attached
it as an "also asked as" variant *only if* its LLM-classified label exactly matched an existing region
label. That exact-string match almost always failed, so pasted queries became stray rows (or were
dropped as near-dups) and nothing showed under any region — with no feedback.

**Product decisions:**
- **A region each** — every distinct pasted query becomes its OWN region (overlapping intents are fine;
  only exact duplicates collapse).
- **Manual first + fill gaps** — the pasted queries' regions are authoritative and lead; the auto
  fan-out then adds ONLY the angles the manual queries didn't cover.

---

## Changes (`generators/post_gen.py`)

### `_regions_from_queries(self, queries)` (new)
Turns pasted queries into region objects, one per distinct query:
- Normalize + drop exact duplicates.
- One Claude call labels each query with a short 2–4 word region name (graceful fallback to a
  query-derived label — labels are cosmetic, rows stay distinct).
- Returns `[{query, region, source:"manual", variants:[], persona:""}]`.

### `create_cluster` — manual-first, fan-out fills only gaps
On a fresh build with observed queries:
- Build the manual regions via `_regions_from_queries(observed)` FIRST.
- Run `_fanout_rewrites(brands, seed, prior_coverage=[manual queries], anchor_summary=…)` — the
  existing `prior_coverage` path makes the fan-out produce DISTINCT NEW rewrites for the *remaining*
  space and filter out anything matching the manual queries. Those are the gap-fill regions
  (`generated`).
- Combine **manual regions first**, then gap-fill; assign personas; persist.
- Return summary counts: `manual_regions`, `gap_regions`, `skipped` (exact-dup pastes).
- Reuse path (existing cluster + observed) routes through the rewritten `_merge_observed`.

### `_merge_observed` — region-per-query (rewritten)
Each pasted query becomes its OWN new `source:"manual"` region; skip only an exact duplicate of a
query already in the cluster (region query or existing variant). Returns `{rewrites, added, skipped}`.
(Used by the "+ Add fan-out" box and the seed+observed generate path.)

### Manual regions are the priority generation targets
- `generate_posts` gap-fill (auto Fill-gaps path, NOT explicit `target_rewrites`): order
  `remaining_gaps` **manual → fixed → generated**, so a run spends its post budget on the user's
  captured queries first.

## UI (`templates/index.html`) + API (`app.py`)
- Cluster display order (`_ord`): **manual first** (manual=0, fixed=1, generated=2) — priority targets
  sit at the top with the ✎ marker.
- `lsubsCreateCluster` toast: `Created "<seed>" — N regions · X from your queries · Y gap-fill · Z duplicates skipped`.
- `lsubsClusterAddFanout` toast: `X regions added from your queries · Z duplicates skipped`.
- `api_cluster_create` passes the new counts straight through from `create_cluster`.

---

## Verification
- `_regions_from_queries` (stubbed labels): N distinct queries → N manual regions, exact dup dropped,
  label fallback when the model returns none.
- `_merge_observed`: each pasted query → its own region; exact dup of an existing region query skipped.
- `create_cluster` (stubbed fan-out + labels): observed queries appear as `source:"manual"` regions
  ordered BEFORE the `generated` gap-fill regions; `prior_coverage` carries the manual queries; counts
  correct (e.g. 3 manual / 2 gap / 1 skipped).
- `python3 ast.parse` on post_gen.py; inline `<script>` `node --check`.

## Notes
- Forward-only: a cluster built the old way keeps its old shape — **delete + recreate** (re-pasting the
  queries) to rebuild each query as its own region with the fan-out filling the rest.
- Reuses `_fanout_rewrites(prior_coverage=…)`, `_assign_personas_to_regions`, `normalize_rewrites`,
  `upsert_ai_search_cluster`. No schema change.
- Files: `generators/post_gen.py`, `app.py`, `templates/index.html`.

---

*End of update spec.*
