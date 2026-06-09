# Botric — Prompt Generation Update #4: Personas + Per-Region Phrasing Variants

**Build specification (standalone) — fourth deliverable.** Adds two complementary upgrades to the
AI-Search root-prompt pipeline, on top of the prior specs. Same stack (Flask + SQLite + Anthropic
Claude). Each section has the real logic/prompt text.

The two additions act at **different layers**, so they compose cleanly:
- **Personas (title-side):** auto-generate a small set of brand-grounded buyer personas (each with a
  brand-FIT flag); use the winnable ones as a LENS in the fan-out to decide *which* questions/regions
  get made + how titles are framed + filter out un-winnable angles; tag each region/post with its
  persona.
- **Per-region phrasing variants (body-side):** every region keeps the real phrasings that formed it
  (Claude fan-out + pasted observed queries + folded duplicates); each region's post is generated
  with *its* phrasings woven into the body → retrievable however that intent is worded.

**Combined region/rewrite object:** `{query, region, source, variants:[str], persona:str}`.

---

## 0. Data model — `db.py`
- New brand column (idempotent migration alongside the other enrichment columns):
  `ALTER TABLE brands ADD COLUMN personas TEXT` — JSON list of persona objects.
- `update_brand(..., personas=None)` param (only writes when passed → manual `context` untouched).
- `normalize_rewrites(raw)` now carries two new fields, backward-compatible:
  - `variants: [str]` (deduped case-insensitively; the region's phrasings) — legacy → `[]`.
  - `persona: str` (the persona this region most represents) — legacy → `""`.

---

## 1. Personas (title-side)

### 1.1 Generate — `generators/brand_enrichment.py`
`generate_brand_personas(claude, name, domain_url, category, audience, use_cases, pain_points)`:
fetch the brand's own homepage (reuse `_fetch_homepage`/`_extract_visible_text`) + a low-temp call.
Returns **3–5 DISTINCT** personas (or `[]` on failure):

```
{label, profile, trigger, goal, constraints, vocab, fit}   # fit ∈ {"yes","maybe","no"}
```

Prompt essentials (grounded, no fabrication, honest fit):
```
You are defining the buyer PERSONAS for a GEO campaign — the distinct kinds of people who would
search for what this brand offers. These decide which recommendation questions are worth targeting
and whether THIS brand is a credible answer for each.
… BRAND NAME / URL / CATEGORY / AUDIENCE / USE-CASES / PAIN-POINTS + HOMEPAGE TEXT …
Produce 3-5 DISTINCT, non-overlapping personas (different situations/intents — not rewordings).
Ground them in the brand's real space; do NOT invent. For each, judge FIT honestly:
  "yes" = squarely the brand's customer · "maybe" = plausible/adjacent ·
  "no"  = wants something the brand isn't (include a couple when realistic — the winnability filter).
Return JSON: {"personas":[{label, profile, trigger, goal, constraints, vocab, fit}]}
```
Normalize: require a non-empty `label`; coerce `fit` to yes/maybe/no (default "maybe"); cap at 5.

### 1.2 Auto-populate (lazy, cached) — `generators/post_gen.py`
`_ensure_personas(self, brands)`: if the primary brand's `personas` is empty → generate, persist via
`db.update_brand(bid, personas=json.dumps(...))`, mutate the in-memory brand. Cached (skip when
present). Called at the top of the AI-Search path in `generate_posts` (`if ai_search: self._ensure_personas(brands)`),
mirroring auto-enrich/grounding. **Lifecycle:** the gate is the `personas` field (not `enriched_at`),
so every existing brand auto-fills on its next AI generate; `POST /api/brands/<id>/personas/regenerate`
clears+rebuilds on demand.

`_fit_personas(self, brands)` → personas with a label and `fit ∈ {yes, maybe}` (drops `fit:no` — the
winnability filter).

### 1.3 Persona LENS in the fan-out (NOT a parallel region taxonomy)
The existing fan-out owns region structure (6 axes + brand-specific, dedup, cap, sparsity). Personas
are a **lens** added to the same `_fanout_rewrites` call — built from `_fit_personas`:

```
PERSONAS — the real askers for this brand (use as a LENS, NOT as separate regions):
  - <label>: <profile> | wants: <goal> | says it like: <vocab>
  • Use them to GROUND the rewrites in how real askers phrase things and to decide which questions
    are worth targeting; SKIP any question no persona above would credibly bring to THIS brand.
  • Do NOT create a region per persona or a persona×axis matrix — still ONE rewrite per DISTINCT
    region. Tag each rewrite with the persona label it most represents (or "(broad)").
```
Omitted entirely when the brand has no (fit) personas (no regression). The fan-out's JSON `rewrites[]`
now also returns `"persona"` per rewrite (parsed into the region object).

### 1.4 Attribution
`persona` flows: fan-out rewrite → cluster `rewrites_json` → the post's `ai_search_meta`
(`{seed, anchor, target_query, persona}`, looked up by the post's `target_query`). Surfaced in the
UI: a 🧑 persona tag on Clusters-tab rewrite rows and in post detail; read-only personas list (label ·
profile · fit badge) + a "↻ Regenerate" button in the Edit-Brand modal.

---

## 2. Per-region phrasing variants (body-side)

### 2.1 Keep the phrasings (don't collapse to one)
- **`_fanout_rewrites` prompt** returns, per region, its close phrasings as `variants` (step "3b. For
  EACH region, also list its VARIANTS — the 2-5 close phrasings/paraphrases an engine would issue for
  that same intent").
- **`_dedup_cap_regions`** no longer drops same-region duplicates — it **folds** the dropped rewrite's
  `query` + `variants` into the kept region's `variants` (one primary query per region; the rest become
  that region's phrasings).
- **`_merge_observed`** — a pasted observed query whose region already exists is **appended to that
  region's `variants`** (instead of skipped); a new region starts a new bucket. (Returns
  `{rewrites, added, enriched, skipped}`.)

### 2.2 Feed each region its own variants into generation
In `_generate_candidates_for_intent`, the AI-Search coverage block now lists each rewrite WITH its
phrasings and instructs the model to weave the *targeted* rewrite's variants into that post's body:
```
REWRITE CLUSTER (the distinct sub-queries the engines fan out to):
  - <rewrite>   [also asked as: <variant 1>; <variant 2>; …]
  …
  • Weave the targeted rewrite's "also asked as" phrasings naturally into that post's BODY, so the
    thread is retrieved however that intent is worded.
```
Implemented by carrying `variants_by_query` (and `persona_by_query`) on `coverage_focus`, built from
the cluster's region objects, so the per-target lookup survives the gap-fill path.

### 2.3 Persist + show
`variants` are stored in the cluster `rewrites_json`; the clusters API emits `variants` + `persona`
per rewrite; the Clusters-tab rewrite row shows the variants ("also asked as: …") and the persona tag.

---

## 3. How the two layers relate
- **Personas → title-side:** which questions/regions exist, the title's framing, winnability (fit),
  and attribution. A *lens* on the existing fan-out — never a per-persona region explosion.
- **Variants → body-side:** the post body matches every real phrasing of its region → maximum
  retrieval.
- Together: personas pick the **right, winnable questions**; variants make each thread **maximally
  findable**.

---

## 4. Build order
1. `db.py`: `personas` migration + `update_brand` param + `normalize_rewrites` (variants, persona).
2. `brand_enrichment.generate_brand_personas`.
3. `post_gen`: `_dedup_cap_regions` fold; `_fanout_rewrites` (variants + persona lens + persona/variants
   in JSON); `_merge_observed` append; `_ensure_personas` + `_fit_personas`; `coverage_focus`
   variants_by_query/persona_by_query; per-region variants block in `_generate_candidates_for_intent`;
   persona into `ai_search_meta`.
4. `app.py`: clusters API emits variants+persona; `POST /api/brands/<id>/personas/regenerate`;
   `add-fanout` returns `enriched`.
5. `templates/index.html`: cluster-row variants + persona tag; post-detail persona; Edit-Brand
   personas list + Regenerate.

## 5. Verification
- Personas: `generate_brand_personas` → 3–5 distinct, fit-honest; `_ensure_personas` persists+caches
  (no refetch on 2nd call); fan-out lens includes fit yes/maybe, EXCLUDES `fit:no`, omitted when none;
  regenerate endpoint clears+rebuilds.
- Variants: `_dedup_cap_regions` folds (not drops); `_merge_observed` appends to an existing region;
  `normalize_rewrites` round-trips variants+persona (legacy → `[]`/`""`); the targeted region's
  variants appear in the generation prompt.
- Attribution: the saved post's `ai_search_meta.persona` is set (verified on the DB row).
- Regression: broad fan-out unchanged; no persona×region explosion; un-enriched / no-persona brand →
  both features cleanly no-op.
- Static: `ast.parse` on db.py / brand_enrichment.py / post_gen.py / app.py; inline `<script>`
  `node --check`.

## 6. Notes
- One Claude call per brand for personas (one-time, cached); brand-level (reused across all
  seeds/clusters). Manual `context` is never written.
- Fully backward compatible: legacy clusters (no variants) and brands (no personas) behave exactly as
  before until regenerated.
- Files: `db.py`, `generators/brand_enrichment.py`, `generators/post_gen.py`, `app.py`,
  `templates/index.html`.

---

*End of update spec.*
