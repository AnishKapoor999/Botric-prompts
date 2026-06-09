# Botric — Prompt Generation Update #3: Title-Quality Gates, Anchor Grounding, Defaults & Viewer

**Build specification (standalone) — third deliverable.** Documents every change made to the
prompt-generation system **since** the *First-Time Coverage + Auto-Enrich* spec. Same stack
(Flask + SQLite + Anthropic Claude). Each section is self-contained with the real logic/prompt
text. Build them on top of the prior two specs.

What's in this update:
1. **Title-quality gates** — titles must be *recommendation questions* AND answerable by naming a
   brand of the brand's own *kind* (no vents/testimonials; no entity-type mismatch).
2. **Anchor-scoped brand grounding** — on each new cluster, learn what the brand actually offers
   for the seed topic, bank it on the brand, and feed it into generation.
3. **Generate-tab defaults** — AI mode on, commercial-only by default.
4. **Grounding viewer** — see the per-seed grounding in the Clusters tab.

---

## 1. Title-quality gates (in `_score_ai_query_relevance`, `_select_best`, `title_rules`, `_fanout_rewrites`)

### 1a. Problem
On the standard (non-AI) route, two kinds of bad titles slipped through:
- **Vents / testimonials / status updates** ("frustrated with doctors…", "started using X — game
  changer") — not recommendation questions.
- **Entity-type mismatches** — e.g. a *clinic* brand getting "best longevity **treatments**" or
  "NAD+ vs Metformin — which works better?", whose answer names **treatments/molecules**, not a
  clinic, so the brand can never be the cited answer.

Both passed because the scorer was (a) only enforcing vent/anchor rules in AI-Search mode, and
(b) **brand-agnostic** (it scored "is this recommendation-seeking?" with no idea what *kind* of
brand it's for).

### 1b. Always-on vent/testimonial rule (route-agnostic)
In `_score_ai_query_relevance`, the base scoring prompt's "Low (1-4)" line now ends with (applies
with or without an anchor):

```
CRITICAL: first-person VENTS, TESTIMONIALS and STATUS UPDATES that don't ASK for anything are
NOT recommendation-seeking — score them 1-4 even if on-topic (e.g. "frustrated with traditional
doctors dismissing X", "so tired of Y", "started using Z — game changer", "X changed my life",
"finally found something that works"). A title only scores high if it explicitly asks for what
to use/buy/try.
```

### 1c. Brand-aware ENTITY-TYPE rule (relative + conservative + gated)
`_score_ai_query_relevance(self, title, body, anchor=None, target_query=None, brand_kind=None)`.
When `brand_kind` is non-empty, append this block to the prompt (and pass
`brand_kind = primary_brand["category"]` from the generate loop to every scorer call):

```
ENTITY-TYPE MATCH (relative to the brand — apply CONSERVATIVELY):
  BRAND KIND: "{brand_kind}". A high-scoring title's natural answer must
  NAME a brand of THIS kind. Cap the score at 3-4 ONLY when the answer would clearly
  name a DIFFERENT kind of thing than the brand — e.g. the brand is a SERVICE /
  PROVIDER / CLINIC but the title's answer is a list of treatments / ingredients /
  molecules / products, or an efficacy "which works better / does X work" comparison
  (those name substances, not a provider, so this brand can't be the cited answer).
  Do NOT cap when the answer's kind MATCHES the brand — including valid same-kind
  comparisons (product vs product for a product brand; clinic vs clinic for a clinic).
  When unsure, do NOT apply this cap.
```

**Generality (critical):** the rule is **relative to the brand's `category`**, **gated** (omitted
entirely when category is empty → no behavior change for un-enriched brands), and **conservative**
("when unsure, do NOT apply"). Product, supplement/treatment, and retailer brands keep their valid
patterns; same-kind comparisons stay allowed. Only a clear cross-kind mismatch caps.

### 1d. Quality floor in `_select_best` (standard route)
`_select_best(self, candidates, requested_storylines, count, threshold=5)`: keep only candidates
scoring `>= threshold`, then do storyline-variety selection **over that passing set** (so a vent
can't be pulled in to fill a `complaint`/`discovery`/`experience` storyline slot). If fewer than
`count` clear the bar, return fewer + log — never pad with weak/vent titles. (The AI-Search route
already had `_select_cluster_best` with threshold 6.)

### 1e. Generation prompt rules (`_generate_candidates_for_intent.title_rules`)
- **Storyline flavors the BODY, not the TITLE** (new rule 5): *"STORYLINE shapes the BODY's
  voice/scenario ONLY — the TITLE is ALWAYS a recommendation question, NEVER a vent, testimonial,
  rant, or status update."* (A "complaint" storyline → body frames the frustration then ASKS what
  to use; the title stays "best X for Y?".)
- **OUR-BRAND CHECK** — keep the existing *specific-product* and *supplier/retailer* branches;
  the title's answer must name the **same KIND** of entity as the brand (its `Category`), and ADD a
  service/provider branch:
  ```
  - SERVICE / PROVIDER / CLINIC (you go to it to GET something done) → the title asks for the
    PROVIDER: the best service / clinic / where to get it / who offers it for the use-case. Do NOT
    frame it as "best <treatments>", "<A> vs <B> — which works better?", or "does <X> work?": those
    are answered with substances / efficacy, not a provider, so this brand can't be the cited
    answer. (A comparison is fine only when it compares PROVIDERS, e.g. "alternative to <competitor
    clinic>".)
  ```

### 1f. Fan-out region constraint (`_fanout_rewrites`)
Add an ENTITY-TYPE rule to the region instructions (so the *cluster regions* themselves don't drift
to treatment-space):
```
2b. ENTITY-TYPE — every rewrite must be a query an AI would answer by naming a brand of THIS
   brand's KIND (see its "Category"); keep every region within that kind. If the brand is a
   SERVICE / PROVIDER / CLINIC, regions are provider-oriented (best service/clinic, where to get
   it / who offers it, alternative to a competitor PROVIDER) — NOT treatment-vs-treatment or
   efficacy ("which works better / does X work") comparisons. (For product / retailer brands this
   is already satisfied — keep the usual product / buy-intent regions.)
```

**Net effect:** a clinic brand anchored on "longevity" yields *"best online longevity clinic for
men"*, *"telehealth for hormone-driven longevity"*, *"alternative to <competitor clinic>"* — not
"best treatments" or "NAD+ vs Metformin".

---

## 2. Anchor-scoped brand grounding (`learned_context`)

### 2a. Why
The fan-out + generation only had the brand's **general** enrichment. Anchoring on an *adjacent*
topic (e.g. a TRT clinic anchored on "longevity") meant the model guessed what the brand offers for
that topic → mismatch/hallucination. Now, on each new cluster, the system **learns what the brand
actually offers for the seed topic** from the brand's own site, **persists it on the brand**
(accumulating, separate from the manual `context`), and **feeds it into generation**.

### 2b. Data model — `db.py`
- New column (idempotent migration): `ALTER TABLE brands ADD COLUMN learned_context TEXT`.
  JSON keyed by `seed_norm`:
  `{ "<seed_norm>": {"anchor": "<seed>", "summary": "...", "covers": true|false, "key_points": [...], "added_at": "<iso>"} }`.
- `update_brand(...)` gains a `learned_context=None` param (same "only writes the params you pass"
  pattern → the manual `context` is never overwritten).

### 2c. Grounding fn — `generators/brand_enrichment.py`
`enrich_brand_for_anchor(claude, name, domain_url, anchor)`: reuse `_fetch_homepage` +
`_extract_visible_text`, then a low-temp Claude call. Returns `{summary, covers, key_points}`
(`{}` on failure). Grounds on the brand's **own site + confident knowledge — NOT the open web** (so
it never invents offerings; returns `covers=false` when there's no real offering). Prompt:

```
You are grounding a GEO campaign. We are about to build content anchored on a specific TOPIC for
this brand, and need to know what THIS brand actually offers or does that is relevant to that
topic — so the content stays truthful and on-target.

BRAND NAME: {name}
BRAND URL: {domain_url}
ANCHOR TOPIC: "{anchor}"

{homepage text, or a "could not fetch — use confident knowledge, else covers=false" note}

Describe ONLY what is supported by the page above or your CONFIDENT knowledge of this brand — do
NOT invent products, services, or claims. If the brand has no real offering relevant to "{anchor}",
say so (covers=false): it's better to flag a poor fit than to fabricate.

Return JSON only:
{ "summary": "1-3 sentences: what this brand offers for \"{anchor}\" (or, if covers=false, a
   one-line note it doesn't serve this topic)", "covers": true|false, "key_points": ["...", "..."] }
```

### 2d. Wiring — `generators/post_gen.py`
- `_ground_brand_for_anchor(self, brands, seed, seed_norm)`: if the primary brand's
  `learned_context` already has `seed_norm` → return cached (no refetch). Else call
  `enrich_brand_for_anchor`, merge `{seed_norm: {...}}`, persist via
  `db.update_brand(bid, learned_context=json.dumps(...))`, **mutate the in-memory brand**, and log a
  weak-fit warning when `covers==false`. Returns the summary string.
- Called at the **top of the `if ai_search and seed_norm:` branch** in `generate_posts` — i.e. on
  every **new** cluster creation (cached → skipped on reuse), **before** `_fanout_rewrites`.
- `_build_enriched_brand_block` renders a **"What the brand offers for specific topics (learned)"**
  section from `learned_context` (covers=true entries) → flows into **both** the fan-out and the
  post prompts. The **current** anchor's summary is also injected straight into `_fanout_rewrites`
  ("WHAT THIS BRAND OFFERS FOR THE SEED TOPIC: …").
- Cost: one extra fetch + Claude call **per new anchor** (cached on the brand), not per post.

---

## 3. Generate-tab defaults (`templates/index.html`, `renderLsubsGenerate`)
- **AI Search mode ON by default** (`<input id="lsubs-ai-search" checked>`).
- **Intent defaults: commercial 2 / comparison 0 / informational 0** (the three `lsubs-ic-*`
  inputs).
- **Seed hint** clarifies: one coherent theme per seed; for separate topics, generate each
  separately (each gets its own cluster/anchor — multi-keyword seeds are NOT auto-split).

---

## 4. Grounding viewer in the Clusters tab (`app.py` + `templates/index.html`)
- **`GET /api/live-posts/clusters`** now attaches a `grounding` field to each cluster:
  `{summary, covers}` looked up from the brand's `learned_context[seed_norm]` (cached per brand so
  each brand's JSON is parsed once). `null` when a cluster has no grounding yet (e.g. created before
  this feature).
- **Clusters-tab card** (`_lsubsRenderClusters`) renders, at the top of each expanded cluster, a
  small panel: a badge — **✓ brand offers this** (green) or **⚠ weak fit** (amber, `covers=false`)
  — plus the grounding **summary** ("what `<seed>` grounding found: …"). HTML-escaped.

---

## Data-model delta (summary)
- `brands.learned_context TEXT` — NEW (JSON keyed by seed_norm; anchor grounding accumulator).
  Manual `context` unchanged; `update_brand` gains the matching param.
- No other schema changes. No new endpoints (the clusters endpoint just gains a `grounding` field).

## Verification
- **Title gates:** vent/testimonial titles score ≤4 (route-agnostic); `_select_best` drops them
  even when a storyline slot wants them; for a clinic `brand_kind`, "best longevity treatments" /
  "NAD+ vs Metformin which works better" are capped and dropped while "best online longevity clinic
  for men" is kept. **No regression:** product/supplement/retailer brands + same-kind comparisons
  are untouched; empty `category` → the entity rule is omitted entirely.
- **Grounding:** generating an AI cluster for a new seed populates `brands.learned_context[seed_norm]`,
  leaves the manual `context` byte-for-byte unchanged, feeds the fan-out + post prompts, logs a
  weak-fit warning when `covers=false`, and does NOT refetch on the next run of the same seed.
- **Defaults:** the Generate tab opens with AI mode checked + 2/0/0.
- **Viewer:** a cluster with grounding shows the summary + the correct badge in the Clusters tab;
  clusters without grounding render normally.
- Static: `python3 -c "import ast; ..."` on `db.py` / `post_gen.py` / `brand_enrichment.py` / `app.py`;
  the single inline `<script>` of `index.html` passes `node --check`.

## Notes
- All title gates are driven by the brand's `category` (auto-enrich populates it; see the
  First-Time/Auto-Enrich spec) — so a brand's first AI generate fills `category` + grounds the
  anchor, then both gates are fully active.
- Everything degrades gracefully when a brand is un-enriched (rules omitted, grounding skipped) —
  nothing blocks generation.

---

*End of update spec.*
