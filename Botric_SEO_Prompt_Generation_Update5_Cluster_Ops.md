# Botric — Prompt Generation Update #5: Persona Distribution Fix + Cluster Ops

**Build specification (standalone) — fifth deliverable.** Three changes to the AI-Search Clusters
feature, on top of the prior specs. Same stack. No schema change.

1. **Persona-distribution fix** — stop the fan-out from tagging every region with the same persona.
2. **Create a cluster without generating posts** — a Clusters-tab action (fan-out only).
3. **Region-classify attached posts** — when you add an existing post to a cluster, route it to the
   right region instead of a generic bucket.

---

## 1. Persona-distribution fix (`generators/post_gen.py` → `_fanout_rewrites`)

**Symptom:** with 4–5 distinct personas on a brand, the fan-out tagged the **first** persona to
*every* region (e.g. all longevity regions → "busy professional dad"). Root cause: the persona-lens
tagging rule was too weak ("tag each rewrite with the persona it most represents"), so the model
blanketed one persona. It's a *distribution* bug, not a persona-count problem (3–5 is the right count).

**Fix:** strengthen the PERSONA TAGGING rule in the lens block:
```
• PERSONA TAGGING: tag each region with the persona whose actual question it is — decide by their
  trigger / goal / vocab, NOT by position in the list. Across the cluster REPRESENT MULTIPLE personas
  — aim for each persona above to drive at least one region where it genuinely fits. NEVER tag every
  region with the same persona (especially not just the first one). Use "(broad)" only when a region
  truly spans several personas.
```
No data-model change. **Forward-only:** existing clusters keep their old persona tags in
`rewrites_json` — to apply the fix, **delete + recreate** the cluster (personas don't need
regenerating if the brand already has a good 3–5).

---

## 2. Create a cluster without generating posts

Previously a cluster only existed as a side-effect of `generate_posts`. Add a fan-out-only path.

**`post_gen.create_cluster(self, brands, seed, observed_queries=None)`** (new):
`_ensure_personas` → `_ground_brand_for_anchor` → `_fanout_rewrites(brands, seed, anchor_summary=…)`
→ optional `_merge_observed` → `db.upsert_ai_search_cluster(...)`. **No post generation.** Returns a
summary `{brand_id, seed, anchor, cluster_size, created, reused}` (or `{error}` if the fan-out yields
nothing). **Reuse-only:** if a cluster already exists for the seed it returns it unchanged (folding in
any `observed_queries`) — never clobbers. To refresh, delete then create.

**`POST /api/live-posts/clusters/create`** (app.py): `{brand_id, seed, observed_queries?}` →
`make_generators()` → `post_gen.create_cluster(...)` → JSON summary (200, or 502 on `error`).

**UI** (`renderLsubsClusters` top bar): a `lsubs-cluster-seed` text input + **"✨ Create cluster"**
button (`lsubsCreateCluster()`) → POST create for the selected brand → spinner → toast (created /
already-existed) → `lsubsLoadClusters()`.

---

## 3. Region-classify attached posts

Previously, adding an existing post by # dumped it into a generic `"(from posts)"` region. Now it's
classified into the right region.

**`db.attach_posts_to_cluster(self, brand_id, seed_norm, post_numbers, region_by_num=None)`** (db.py):
when `region_by_num` (`{post_number: region}`) is supplied, for each post:
- region matches an **EXISTING** cluster region → the post **covers** it: its
  `ai_search_meta.target_query` is set to that region's primary query, and the post's title is added
  to that region's `variants` (a real phrasing);
- else → a **NEW** rewrite is added `{query: title, region: <classified or "(from posts)">,
  source: "manual", variants: [], persona: ""}` and the post targets it.
Returns `{attached, not_found, existing:[…], new:[…], cluster_size}`. (Without `region_by_num`, the
old "(from posts)" behavior remains.)

**`api_cluster_add_posts`** (app.py): now uses `make_generators()`; loads the cluster's existing region
labels; fetches the attached posts' titles; calls `post_gen._classify_regions(titles, existing_regions)`
(the same classifier the observed-paste path uses) → builds `region_by_num` → passes it to
`attach_posts_to_cluster`. 404s if no cluster exists for the seed.

---

## Verification
- **Persona distribution:** the fan-out prompt contains the "represent multiple personas / never tag
  every region with the same persona" rule (unit asserts the text); a multi-persona brand yields
  varied `persona` tags across regions.
- **Create cluster:** `create_cluster` persists a cluster (rewrites + variants + personas) with **0
  posts**; second call for the same seed is `reused`/idempotent; endpoint + button surface it in the
  Clusters tab.
- **Attach classify:** a post whose title matches an existing region → `existing`, its `target_query`
  set to that region + title added as a variant; an off-topic post → `new` region. (Unit-verified via
  a stubbed `_classify_regions` + temp DB.)
- `python3 ast.parse` on db.py / post_gen.py / app.py; inline `<script>` `node --check`.

## Notes
- Reuses `_ensure_personas`, `_ground_brand_for_anchor`, `_fanout_rewrites`, `_merge_observed`,
  `_classify_regions`, `upsert_ai_search_cluster`, `normalize_rewrites`.
- Backward compatible; no schema change.
- Operational note: to fix an *already-built* cluster that has the uniform-persona bug, **delete it
  and Create it again** (the new fan-out re-distributes personas).
- Files: `db.py`, `generators/post_gen.py`, `app.py`, `templates/index.html`.

---

*End of update spec.*
