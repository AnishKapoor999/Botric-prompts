# Botric — Prompt-Generation System

**Build specification & handover document**

This document is a self-contained spec for building a local prompt-generation tool. A
developer should be able to produce the entire system from this file alone, with no access
to the original codebase. It reproduces, 1:1, three things from the reference bot:

1. **Add a brand by URL** → auto-fetch & extract brand context (the exact format used today)
   → **plus detect multiple ICPs** → review/edit → save.
2. **Prompt generation** exactly as the reference bot does it (the AI-Search *region /
   sparse-fan-out → cluster → generate* pipeline).
3. **UI** that follows the current client-dashboard look & feel (teal + slate, Inter, cards).

---

## 0. Decisions & scope (read first)

| Topic | Decision |
|---|---|
| **Purpose** | Generate Reddit-style GEO *prompts* (post titles + bodies) for a brand. |
| **Operators** | Botric team. **No authentication.** Single-user, runs **locally**. |
| **Scope** | Prompt generation **only**. |
| **Out of scope** | Reddit posting, HQ/anchor comments, OP-affirm, scheduling, "check-live" status tracking, multi-tenant clients/login. |
| **ICPs** | **Multiple structured ICPs**, *storage-only*: detected, stored, editable, and folded into the brand-context block that generation reads (the same loose way `audience` is used). ICPs are **not** a new fan-out axis. |
| **Stack** | **Replicate exactly**: Python **Flask** + **SQLite** (WAL) + **Anthropic Claude** (`claude-sonnet-4-20250514`). **Optional** OpenAI embeddings for a relevance gate (off unless a key is set). |

> **Terminology.** A "prompt" here = a generated Reddit post (a `title` + `body`) written to
> mirror the long-tail question a real user types into ChatGPT/Perplexity/Gemini. The brand
> is **never** named in the prompt itself — that's intentional (the recommendation later lives
> in a comment, which is out of scope for this tool). The post is the *retrieval surface*.

---

## 1. Overview & goal

### What GEO prompt-coverage is (one paragraph)
AI assistants answer "what should I use for X?" by **rewriting** the user's prompt into a few
search sub-queries ("query fan-out") and retrieving content **semantically** (by meaning, not
exact words). Real fan-out is **sparse** (an engine issues ~2–6 sub-queries, not dozens) and
**non-deterministic** in wording, but across engines and repeats it **converges on the same
handful of distinct REGIONS** (buyer-concern angles). So to be the recommended answer, you
**cover the regions**, not exact strings: one natural Reddit thread per region, each packed
with that region's phrasings so it gets retrieved. This tool produces those threads.

### What the tool does
```
Add brand (URL) ─► Enrich (context + GEO fields + ICPs) ─► review/edit ─► save
       │
       ▼
Open brand ─► "Generate prompts": enter a SEED + count + intent split
       │
       ▼
Fan the seed out into distinct REGIONS ─► build/reuse a persisted CLUSTER
       │
       ▼
Generate ONE strong prompt per region × intent ─► score ─► coverage-gate ─► (optional) embedding-gate
       │
       ▼
Review cluster (coverage / gaps) ─► fill gaps / generate-selected / paste observed / add manually / delete
       │
       ▼
View & export the generated prompts (CSV)
```

### Non-goals (explicit)
No Reddit API, no posting/scheduling, no comments/anchors, no status tracking, no login/clients.
The deliverable is **generated prompts, stored and exportable**.

---

## 2. System at a glance

```
┌──────────────────────────────────────────────────────────────┐
│  Browser (operator) — single-page UI styled like the portal   │
└───────────────┬──────────────────────────────────────────────┘
                │ JSON over HTTP (localhost)
┌───────────────▼──────────────────────────────────────────────┐
│  Flask app (app.py)                                            │
│   • /api/brands*          (CRUD + enrich)                      │
│   • /api/prompts/generate (async task)                        │
│   • /api/clusters*        (read / gap-fill / observed / etc.)  │
│   • /api/tasks/<id>       (poll background generation)         │
│   • in-process task registry (threads)                         │
├───────────────┬───────────────────────┬──────────────────────┤
│  db.py        │  generators/          │  config.py            │
│  SQLite (WAL) │   brand_enrichment.py │  keys, model, consts  │
│               │   post_gen.py         │                       │
└───────────────┴───────────────────────┴──────────────────────┘
        │                    │
        ▼                    ▼
   strategy_bot.db     Anthropic Claude API
   (local file)        OpenAI embeddings (optional)
```

**Suggested file layout** (mirrors the reference):
```
prompt-gen/
  app.py                      # Flask routes + background task registry
  db.py                       # SQLite schema + all data access
  config.py                   # env loading, model, constants
  generators/
    __init__.py
    base.py                   # ClaudeClient wrapper, banned phrases
    brand_enrichment.py       # website fetch + GEO extraction (+ ICPs)
    post_gen.py               # the fan-out → cluster → generate pipeline
  templates/
    index.html                # the single-page operator UI
  static/                     # logo, css if any
  requirements.txt
  .env                        # ANTHROPIC_API_KEY=..., optional OPENAI_API_KEY=...
```

**Run model:** `python app.py` → serves on `http://127.0.0.1:5000`. SQLite file is created on
first run; WAL mode for concurrent reads during background generation. No auth middleware.

---

## 3. End-to-end operator flow (the intended UX)

1. **Add brand.** Operator enters a **website URL** (and optionally a name) → clicks **✨ Enrich**.
   The server fetches the homepage, extracts visible text, and asks Claude for the structured
   GEO fields **plus a list of ICPs**. The draft pre-fills the form. Operator reviews/edits
   (especially ICPs) → **Save**.
2. **Open the brand → Generate prompts.** Operator **optionally** types a **seed** (a root
   prompt/question, a few of them, or just a platform/keyword — e.g. *"any good background music
   tools for Instagram Reels?"* or *"Instagram"*). The seed is **not required** — leave it blank
   and the fan-out derives the anchor from the brand's most central use-case. Operator picks a
   **count** and an **intent split** (commercial / comparison / informational), and clicks
   **Generate**.
3. **Fan-out + cluster.** The server fans the seed into **distinct regions** (6 fixed axes +
   any brand-specific ones, sparse, deduped, capped), and **persists the cluster** keyed by
   `(brand, seed)`. Re-running the same seed **reuses** the cluster and **fills gaps** instead
   of repeating angles.
4. **Generate.** For each intent it drafts candidate titles+bodies targeting the uncovered
   regions, scores them (anchor- and recommendation-aware), keeps the strongest **distinct**
   one per region (coverage gate), and — if an OpenAI key is set — drops any whose body drifts
   semantically from its target query (embedding gate). Results are saved as prompts.
5. **Review the cluster.** A Clusters view shows each region, whether it's **covered**, the
   **gaps**, and exact `X/N` coverage. Operator can **fill gaps**, **generate for selected
   regions**, **paste observed queries** (real fan-out captured from ChatGPT/Gemini, deduped by
   region), **add existing prompts to the cluster by number**, or **delete** the cluster.
6. **View & export.** A prompts view lists generated prompts with seed / region / intent
   columns and a **CSV export**.

---

## 4. Module 1 — Brands & ICPs

### 4.1 Brand data model

Base table (note: `subreddit_id` in the reference is a deployment artifact — **make it
nullable/optional** here since deployment is out of scope; keep it only as an optional
"target subreddits" hint used to flavor generation):

```sql
CREATE TABLE IF NOT EXISTS brands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    domain_url   TEXT,
    context      TEXT NOT NULL,          -- the 3-5 sentence human-readable description
    keywords     TEXT,                   -- JSON array of strings
    added_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(name)
);
```

Enrichment columns (added as idempotent migrations in the reference — fold them into the
CREATE or run as `ALTER TABLE ... ADD COLUMN`):

```sql
ALTER TABLE brands ADD COLUMN category      TEXT;   -- "best music tool for video creators"
ALTER TABLE brands ADD COLUMN audience      TEXT;   -- single ICP sentence (legacy; kept)
ALTER TABLE brands ADD COLUMN use_cases     TEXT;   -- JSON array of short phrases
ALTER TABLE brands ADD COLUMN pain_points   TEXT;   -- JSON array of short phrases
ALTER TABLE brands ADD COLUMN features      TEXT;   -- JSON array of short phrases
ALTER TABLE brands ADD COLUMN competitors   TEXT;   -- JSON array of real competitor names
ALTER TABLE brands ADD COLUMN enriched_at   TEXT;   -- ISO timestamp of last enrichment
ALTER TABLE brands ADD COLUMN icps          TEXT;   -- NEW: JSON array of ICP objects (see 4.3)
ALTER TABLE brands ADD COLUMN target_subreddits TEXT; -- optional JSON array (flavor only)
```

> List fields are stored as **JSON strings** and parsed on read. `context`, `category`,
> `audience` are plain strings.

### 4.2 Website enrichment (exact reference behavior)

Three steps, all graceful (a failed homepage fetch must **not** error — Claude falls back to
the brand name + general knowledge).

**(a) Fetch homepage** — `requests.get`, browser-ish User-Agent, 10s timeout, follow
redirects, returns `""` on any exception or non-200:

```python
def _fetch_homepage(domain_url, timeout=10):
    if not domain_url: return ""
    url = domain_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        resp = requests.get(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and resp.text:
            return resp.text
    except requests.exceptions.RequestException as e:
        print(f"[enrich] fetch error for {url}: {e}")
    return ""
```

**(b) Extract visible text** — stdlib `html.parser.HTMLParser` subclass that skips
`{script, style, noscript, svg, template, iframe}`, collapses whitespace, and **caps at 6000
chars**. (No BeautifulSoup dependency.)

**(c) Ask Claude** — `max_tokens=1500, temperature=0.3`, expects JSON back. **Use this exact
prompt**, extended with the `icps` field (the only change vs the reference is the two ICP
lines + the `icps` array in the return shape):

```
You are analyzing a brand to extract structured context for a GEO (Generative
Engine Optimization) content strategy. The goal is to write Reddit posts that mirror
the long-tail questions real users type into ChatGPT/Perplexity about this brand's
domain — WITHOUT naming the brand itself.

BRAND NAME: {name}
BRAND URL: {domain_url or "(none)"}

{page_section}          # the homepage text, or a "could not fetch" note (see below)

Extract the following fields. Be specific and concrete — vague answers are useless.

- category: A precise product category, 3-8 words. Example: "project management SaaS for remote teams", "direct-to-consumer electric toothbrush". Not "software" or "product".
- audience: The ideal customer profile (ICP). Who buys/uses this? Role, team size, industry, context. 1-2 sentences.
- use_cases: 4-6 concrete jobs-to-be-done — what problems do users hire this product to solve? Each item should be a short phrase ("running async standups across timezones"), not a sentence.
- pain_points: 4-6 concrete pains the product addresses. Each item is a phrase ("tickets get lost between Slack and Jira"). These are the pains real users would complain about on Reddit.
- features: 4-6 key differentiating features or capabilities. Each is a short phrase.
- competitors: 3-8 direct competitor brand/product NAMES (real names like "Notion", "Asana", "Linear"). These will be used in comparison-intent posts, so accuracy matters. If you're unsure, include fewer but only confident ones.
- icps: 3-5 DISTINCT ideal customer profiles — the different kinds of buyers/users for this brand. Each is an object {label, role, segment, context, pains}: label = a 2-4 word name ("solo podcaster"); role = who they are; segment = market/size/industry; context = the situation in which they need this; pains = 1-3 short pain phrases specific to THIS profile. Make the profiles genuinely different from each other, not rewordings.
- context_summary: A 2-3 sentence narrative describing what the brand is and who it serves. This replaces or augments the existing brand context field.

Return JSON only, exactly this shape:
{
  "category": "string",
  "audience": "string",
  "use_cases": ["string", ...],
  "pain_points": ["string", ...],
  "features": ["string", ...],
  "competitors": ["string", ...],
  "icps": [{"label":"string","role":"string","segment":"string","context":"string","pains":["string", ...]}, ...],
  "context_summary": "string"
}
```

`page_section` is built as:
- if text was fetched: `HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""`
- else: `HOMEPAGE TEXT: (could not fetch homepage — rely on the brand name and your general
  knowledge; flag uncertainty by leaving fields as empty strings or empty arrays.)`

**Normalize the result** before returning the draft: coerce list fields to lists of trimmed
non-empty strings, string fields to trimmed strings, `icps` to a list of dicts (drop malformed
entries), and add `"_page_fetched": bool(page_text)` so the UI can warn when the homepage was
unreachable. The enrich endpoint **returns a draft — it never saves.** The operator reviews,
then a normal create/update persists it.

### 4.3 ICPs (the one genuinely new piece)

The reference bot has only the single `audience` string. Here ICPs are a **first-class,
multiple, editable** field but **storage-only** in their effect on generation:

- **Detected** by the extended enrichment prompt (the `icps` array above).
- **Stored** in `brands.icps` as a JSON array of `{label, role, segment, context, pains[]}`.
- **Editable** in the UI (add / remove / edit rows).
- **Used loosely:** when generation builds its brand-context block (see §5.2,
  `_build_enriched_brand_block`), append a compact ICP summary line, exactly as `audience` is
  appended today — e.g. `  ICPs: solo podcaster; small agency editor; course creator`. ICPs do
  **not** create new fan-out regions and do **not** change the region axes.

### 4.4 Add / Edit brand UI & endpoints

- **Add-Brand form:** Website URL + **✨ Enrich** button; Brand Name; Context (textarea);
  Keywords; a collapsible **"GEO fields"** section (Category, Audience, Use cases, Pain points,
  Features, Competitors — one-per-line or comma textareas); and an **ICP editor** (repeatable
  rows: label / role / segment / context / pains).
- **Enrich handler:** `POST /api/brands/enrich {name, domain_url}` → draft → pre-fill all
  fields. Only overwrite Context if it's empty (or on explicit confirm). Toast whether the page
  was fetched (`_page_fetched`).
- **Save (create):** `POST /api/brands` with `{name, domain_url, context, keywords[],
  category, audience, use_cases[], pain_points[], features[], competitors[], icps[],
  target_subreddits[]}` → set `enriched_at` if any GEO field is populated.
- **Edit:** `PUT /api/brands/<id>` with the same body; `POST /api/brands/<id>/enrich`
  re-enriches an existing brand using its stored name + URL.

---

## 5. Module 2 — Prompt generation (exact pipeline)

This is the core. Implement it faithfully; the algorithm and the prompts below are the product.

### 5.1 Concepts

- **Query fan-out is sparse + non-deterministic but region-stable.** Don't generate many
  synonyms of one query — generate **one** prompt per **distinct region**.
- **The 6 fixed region axes** (the recurring dimensions of "recommend me an X" queries). Claude
  instantiates the ones that apply to a given brand/seed, may **skip** ones that don't fit, and
  may add up to **2 brand-specific** regions:

  ```python
  FIXED_REGIONS = [
      "Category / best tool",
      "Comparison / alternative",
      "Constraint",            # the dominant buying constraint (copyright, budget, compliance…)
      "Use-case / workflow",
      "Persona / segment",
      "Adjacent platform",
  ]
  MAX_FANOUT_REWRITES = 10     # hard ceiling: one per region, keeps the cluster sparse
  ```
- **Anchor.** The campaign's core platform/use-case (e.g. *"Instagram Reels"*). Every generated
  title must stay on the anchor; it may broaden only to a **named** adjacent variant (Reels →
  Shorts), never to something generic ("my videos").
- **The query is the TARGET; the post is what lands on it.** The engine's literal query is
  keyword-ish machine-speak; a verbatim title looks like spam. So **the title reads like a real
  person**, and the **query's literal wording lives in the BODY** (for keyword/BM25 match)
  alongside its paraphrases (for embedding match).

### 5.2 Region fan-out — `_fanout_rewrites(brands, seed=None, prior_coverage=None)`

One Claude call (`max_tokens=1500, temperature=0.7`) that returns the cluster. `prior_coverage`
(a set of already-covered sub-queries) steers it to produce **distinct new** rewrites for the
remaining space and filters out anything matching what's covered — that's what makes "generate
more" extend a cluster instead of repeating it.

**Exact prompt** (`brand_block` is the enriched brand context incl. ICPs; `target_names` are the
brand names to never emit; `seed_line` injects the seed and, when present, the ALREADY-COVERED
block):

```
You are mapping the AI-search query space for a GEO campaign. The goal is to get
this brand recommended by AI assistants. AI engines REWRITE a user's prompt into
several search sub-queries ("query fan-out") and retrieve SEMANTICALLY, so we need
the full cluster of phrasings around the brand's buying/recommendation intent —
NOT one literal phrasing.

BRAND CONTEXT (ground the queries here; NEVER output the target brand name(s): {target_names}):
{brand_block}
{seed_line}
Do this in your head, then return the merged result:
  1. ANCHOR — identify the core platform / use-case this campaign targets. If the
     seed names a platform or use-case (e.g. "Instagram Reels", "podcast intros",
     "online store checkout"), THAT is the anchor — extract it exactly. If there's
     no seed, derive the anchor from the brand's single most central use-case/
     platform. The anchor is short (1-4 words). Every rewrite stays on this anchor;
     a rewrite may broaden to a NAMED adjacent variant (e.g. Reels → Shorts /
     short-form) but never collapses to something generic ("my videos", "content").
  2. REGIONS — real AI engines fan a prompt into only a FEW sub-queries (ChatGPT
     ~2-3, Perplexity ~3-6, Gemini ~3-5), and across engines they converge on the
     same handful of DISTINCT regions, NOT a dozen synonyms. Produce ONE rewrite per
     region. Consider these 6 standard axes and instantiate the ones that apply to
     THIS brand/seed (SKIP any that don't fit); fill the "Constraint" slot with this
     brand's dominant buying constraint (copyright/licensing, price/free, compliance,
     integration, speed…):
       - Category / best tool      → "best X for Y"
       - Comparison / alternative  → "X vs Y", "alternative to <competitor>"
       - Constraint                → the #1 buying concern (e.g. copyright-safe, budget)
       - Use-case / workflow        → the specific job (auto-sync to video, for podcasts)
       - Persona / segment          → for small business, for beginners/pros
       - Adjacent platform          → a NAMED adjacent (Reels → Shorts / TikTok)
     You MAY add up to 2 brand-specific regions the 6 don't capture.
  3. Write ONE rewrite per region — phrased the way the engines actually search
     (short, keyword-ish, real intent), distinct from the others. HARD RULE: if two
     rewrites would get essentially the SAME AI answer, keep only ONE. Aim for
     ~5-8 rewrites TOTAL — fewer, distinct regions beat many synonyms.
  4. Produce a concise CONCEPT/PHRASING CHECKLIST: the key natural-language
     phrasings, synonyms and domain terms a Reddit thread should contain so it
     matches the whole cluster for both keyword (BM25) and embedding retrieval.

Return JSON only:
{
  "anchor": "the platform/use-case to keep every title on (short)",
  "rewrites": [
    {"query": "the sub-query, engine-style", "region": "one of the 6 axis names OR a brand-specific region", "fixed": true/false (true ONLY if it's one of the 6 standard axes)},
    ...
  ],
  "checklist": ["phrasing/term 1", "phrasing/term 2", "..."]
}
Aim for 5-8 rewrites total. Never include the target brand name.
```

`seed_line` (when a seed is given):
```
ANCHOR SEED (expand the fan-out AROUND this — an existing prompt/question, several, or a keyword/platform):
{seed}
```
and when `prior_coverage` is given, append:
```
ALREADY COVERED — these sub-queries are already handled by existing threads. Produce
DISTINCT NEW rewrites for the REMAINING space (other sub-use-cases / buyer concerns /
phrasings). Do NOT repeat or lightly reword any of these:
  - {covered query 1}
  - {covered query 2}
  ...
```

**Post-processing** (deterministic, in code — guarantees sparsity even if the model over-produces):

- Parse each rewrite to `{query, region, source}` where `source = "fixed"` if `fixed` else
  `"generated"`; default region `"(unsorted)"`.
- Drop any query matching `prior_coverage` (case-insensitive).
- Run `_dedup_cap_regions` (below).
- Return `{"anchor": str, "rewrites": [...], "checklist": [...]}` or `None` on total failure
  (callers treat `None` as "no steering" and fall back to the standard path).

```python
@staticmethod
def _dedup_cap_regions(rewrites):
    # Keep ONE rewrite per real region (fixed-source preferred); exempt "(unsorted)";
    # cap at MAX_FANOUT_REWRITES with fixed regions first.
    ordered = sorted(rewrites, key=lambda r: 0 if r.get("source") == "fixed" else 1)
    seen_region, out = set(), []
    for r in ordered:
        region = (r.get("region") or "(unsorted)").strip()
        key = region.lower()
        if region != "(unsorted)" and key in seen_region:
            continue
        if region != "(unsorted)":
            seen_region.add(key)
        out.append(r)
    return out[:MAX_FANOUT_REWRITES]
```

### 5.3 Capturing real observed fan-out (optional but powerful)

Operators can paste **real** sub-queries they saw an engine emit. Fold them in **deduped by
region** so you only add genuinely new angles:

- `_classify_regions(queries, existing_regions)` — one Claude call (`max_tokens=512,
  temperature=0.2`) that maps each pasted query to a region (reusing an existing label verbatim
  when it fits, else naming a new 2–4 word region). Returns `[{query, region}]`.
- `_merge_observed(rewrites, observed_queries)` — classify, then add a query **only if** its
  region isn't already present and the query isn't a duplicate; tag added ones `source="manual"`.
  Returns `{rewrites, added[], skipped[]}` (skips carry a reason: "duplicate query" / "region
  already covered").

### 5.4 Cluster model & lifecycle

The fan-out result is **persisted** per `(brand, seed)` so coverage is computed against a
**stable** cluster (exact `X/N`) instead of re-derived each run.

```sql
CREATE TABLE IF NOT EXISTS ai_search_clusters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id       INTEGER,
    seed_norm      TEXT NOT NULL,         -- normalized seed (lowercase, trimmed, collapsed ws)
    seed           TEXT,                   -- original seed text (for display)
    anchor         TEXT,
    rewrites_json  TEXT,                   -- JSON: [{query, region, source}]
    checklist_json TEXT,                   -- JSON: [str]
    backfilled     INTEGER DEFAULT 0,      -- 1 = reconstructed from existing posts
    created_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(brand_id, seed_norm)
);
```

**`normalize_rewrites(raw)`** — backward-compatible reader: accepts a JSON string or list, a
legacy flat `[str]` **or** the new `[{query, region, source}]`, and returns the object form,
deduped by query (case-insensitive). (Include it so old/imported data still loads.)

```python
@staticmethod
def normalize_rewrites(raw):
    if isinstance(raw, str):
        try: raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError): raw = []
    out, seen = [], set()
    for r in (raw or []):
        if isinstance(r, dict):
            q = (r.get("query") or "").strip()
            region = r.get("region") or "(unsorted)"
            source = r.get("source") or "generated"
        else:
            q = str(r).strip(); region, source = "(unsorted)", "generated"
        k = q.lower()
        if q and k not in seen:
            seen.add(k); out.append({"query": q, "region": region, "source": source})
    return out
```

**DB methods to implement** (names match the reference):
- `normalize_seed(seed)` → lowercase/trim/collapse-whitespace key.
- `get_ai_search_cluster(brand_id, seed_norm)` / `get_ai_search_clusters_for_brand(brand_id=None)`.
- `save_ai_search_cluster(...)` (INSERT OR IGNORE) and `upsert_ai_search_cluster(...)` (UPDATE
  if exists else INSERT) — store `rewrites_json`, `checklist_json`, `anchor`, `seed`.
- `delete_ai_search_cluster(brand_id, seed_norm)`.
- `get_covered_target_queries(brand_id, seed_norm)` → the set of `target_query` values already
  used by generated prompts for this cluster (drives gap computation & `prior_coverage`).
- `backfill_clusters_from_posts(brand_id=None)` → reconstruct cluster rows from previously
  generated prompts (rewrites = covered-only, `source="manual"`, `backfilled=1`).
- `attach_posts_to_cluster(brand_id, seed_norm, post_numbers)` → manually fold existing prompts
  (by their per-brand number) into a cluster.

**Coverage vs gaps.** For a cluster, *covered* rewrites = those whose `query` appears in
`get_covered_target_queries`; *gaps* = the rest. "Generate more" passes the covered set as
`prior_coverage` to the fan-out (extend, don't repeat) and targets the gaps.

### 5.5 Generation — `generate_posts(...)`

Signature (keep it identical; the AI-Search params are what this tool uses):

```python
def generate_posts(self, subreddit, brands, count=None, custom_topics=None,
                   intent_counts=None, context_only=False, seed=None,
                   ai_search=False, observed_queries=None, target_rewrites=None):
    ...
```

- **Intents.** Posts are balanced across `INTENT_TYPES = ("commercial", "comparison",
  "informational")`. Two sizing modes:
  - `intent_counts={"commercial":2,"comparison":0,"informational":3}` — exact per-intent counts
    (the flexible mode the UI uses; intents with 0 are skipped).
  - `count` — legacy strict 1:1:1 batch, must be in `POST_BATCH_SIZES = (3, 6, 9)`.
> **Seed is optional.** `seed` defaults to `None` everywhere. In AI-Search mode a `None`/blank
> seed means `_fanout_rewrites` derives the anchor from the brand's single most central
> use-case/platform and still produces a full region cluster (see §5.2). The standard
> (`ai_search=False`) path uses **no seed at all** — it generates straight from brand context.
> A seed simply focuses the fan-out around a specific prompt/platform when you have one.

- **AI-Search flow** (when `ai_search=True`):
  1. Resolve/seed the cluster: reuse the persisted cluster for `(brand, seed)` if present
     (folding in `observed_queries` via `_merge_observed`), else call `_fanout_rewrites(brands,
     seed, prior_coverage=covered)` — `seed` may be `None`, in which case the anchor is
     brand-derived — then `upsert` it. (`seed_norm` for a blank seed is just the empty-string
     key, so a brand has one "default" cluster plus one per named seed.)
  2. Determine the **target rewrites**: if `target_rewrites` is given (operator picked specific
     regions/queries), target **exactly** those; else target the current **gaps**.
  3. For each intent, generate candidate titles+bodies via
     `_generate_candidates_for_intent(...)`, passing the anchor, the per-candidate
     `target_query`, and the checklist so bodies weave the cluster phrasings (query wording in
     the body, natural title).
  4. **Score** each candidate with `_score_ai_query_relevance(title, body, anchor, target_query)`.
  5. **Coverage-gate** with `_select_cluster_best(candidates, count, threshold=6)` — keep the
     strongest **distinct** rewrite per slot; never pad with duplicates (log the shortfall).
  6. **Embedding-gate** (optional) `_embedding_gate(candidates)` **before** selection — drop
     candidates whose `title+body` is below `EMBED_THRESHOLD` cosine to their `target_query`.
     No-op when no OpenAI key.
  7. **Persist** the kept prompts and update the cluster.

**Scoring prompt** — `_score_ai_query_relevance(title, body, anchor=None, target_query=None)`
returns 0–10 (`max_tokens=256, temperature=0.3`; default 5 on parse failure). It rewards titles
an AI would answer by **naming a product**, and in AI-Search mode it additionally **caps** the
score for anchor drift (≤4) or vent/statement form (≤5):

```
You are scoring a Reddit post TITLE for a GEO campaign whose goal is to get a
specific product/service recommended by AI assistants (ChatGPT / Perplexity / Google).

Title: "{title}"
Body preview: "{body[:200]}"

Rate 0-10 on BOTH dimensions together:
  (1) How likely a real person types this exact question (or close paraphrase) into an AI / search engine, AND
  (2) Whether the NATURAL ANSWER is to RECOMMEND a specific product / brand / service to use or buy.

High (8-10): Clearly recommendation-seeking — a helpful AI would answer by naming specific products/services/suppliers ("best X for Y", "which X should I use for Z", "go-to X for Y", "alternative to X for Y", "where to buy X online", "best place to order X", "who sells X").
Medium (5-7): Advice-seeking that MIGHT surface a product recommendation ("has anyone tried X", "what do you use for Y").
Low (1-4): Generic information / efficacy / how-it-works / "what to look for" / concept questions where the answer is an EXPLANATION rather than a product recommendation (e.g. "do X actually work", "how does X work", "what is X"); also rants, memes, very personal one-offs.

AI-SEARCH MODE — additionally enforce (these can CAP the score):
  ANCHOR = "{anchor}". The title MUST keep this platform/use-case (present or
  unmistakably implied). If the title has drifted to a generic phrasing that drops
  the anchor (e.g. "...for my videos" when the anchor is "Instagram Reels"), cap the
  score at 4 — it's off-target and weak.
  QUESTION FORM. The title must read as a recommendation QUESTION, not a vent or
  bare statement ("so tired of X", "need better Y"). A statement/vent caps at 5
  even if on-anchor.
  This title is supposed to target the cluster sub-query: "{target_query}".
  A strong title is: on-anchor + a recommendation question + cleanly maps to its
  target sub-query.

Return JSON only:
{"score": 0-10, "reasoning": "brief explanation"}
```

**Coverage-gated selection** — `_select_cluster_best(candidates, count, threshold=6)`:
```python
strong = [c for c in candidates if c.get("ai_query_score", 0) >= threshold]
strong.sort(key=lambda c: c.get("ai_query_score", 0), reverse=True)
selected, seen = [], set()
for c in strong:                       # one strong post per DISTINCT target_query
    key = (c.get("target_query") or c.get("title") or "").strip().lower()
    if key in seen: continue
    seen.add(key); selected.append(c)
    if len(selected) >= count: break
# Prefer distinct coverage over hitting `count`: if too few distinct strong rewrites
# exist, return fewer and LOG it — never silently pad with duplicates.
return selected[:count]
```

### 5.6 Embedding relevance gate (optional, graceful)

A deterministic check that a generated post is provably on-target for its query. **Off** unless
`OPENAI_API_KEY` is set; on error it returns candidates unchanged (never blocks generation).
Plain HTTP — no SDK.

```python
def _embed_texts(self, texts):
    if not OPENAI_API_KEY or not texts: return None
    try:
        resp = requests.post("https://api.openai.com/v1/embeddings",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": EMBED_MODEL, "input": texts}, timeout=20)
        if resp.status_code != 200: return None
        data = resp.json().get("data") or []
        if len(data) != len(texts): return None
        return [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]
    except Exception: return None

@staticmethod
def _cosine(a, b):
    if not a or not b: return 0.0
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a)); nb = math.sqrt(sum(y*y for y in b))
    return dot/(na*nb) if na and nb else 0.0

def _embedding_gate(self, candidates):
    cands = [c for c in candidates if (c.get("target_query") or "").strip()]
    if not cands: return candidates
    qv = self._embed_texts([c["target_query"].strip() for c in cands])
    pv = self._embed_texts([((c.get("title") or "") + " " + (c.get("body") or "")).strip() for c in cands])
    if not qv or not pv: return candidates            # gate inactive → pass all
    sim = {id(c): self._cosine(q, p) for c, q, p in zip(cands, qv, pv)}
    return [c for c in candidates if sim.get(id(c), 1.0) >= EMBED_THRESHOLD]
```

### 5.7 Where prompts are stored

Save generated prompts to a `posts` (you may call it `prompts`) table. Reference columns to keep:

```sql
CREATE TABLE IF NOT EXISTS posts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand_id        INTEGER REFERENCES brands(id),
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    intent          TEXT,                  -- commercial | comparison | informational
    ai_query_score  INTEGER DEFAULT 0,
    ai_search_meta  TEXT,                  -- JSON {seed, anchor, target_query}
    concept_checklist TEXT,                -- JSON [str] (the cluster checklist)
    post_number     INTEGER,               -- stable per-brand display number (1,2,3…)
    prompt_version  TEXT,                  -- e.g. "v1.0-ai-search"
    target_subreddit TEXT,                 -- optional flavor
    created_at      TEXT DEFAULT (datetime('now'))
);
```

- `ai_search_meta` ties each prompt to its cluster (`seed`) and the exact `target_query` it
  covers — that's what `get_covered_target_queries` reads for coverage/gaps.
- `post_number` is a stable per-brand counter (assigned at save, backfillable), used everywhere
  in the UI to identify a prompt and to "add by number" to a cluster.

### 5.8 API surface (request/response shapes)

| Method & path | Body | Returns |
|---|---|---|
| `POST /api/brands/enrich` | `{name, domain_url}` | enrichment **draft** (incl. `icps`, `_page_fetched`) — not saved |
| `POST /api/brands` | full brand body (§4.4) | created brand |
| `PUT /api/brands/<id>` | partial brand body | updated brand |
| `POST /api/brands/<id>/enrich` | – | re-enrich draft for an existing brand |
| `POST /api/prompts/generate` | `{brand_id, seed?, ai_search:true, intent_counts, count?, observed_queries?, target_rewrites?}` (`seed` optional → brand-derived anchor) | `{task_id}` (async) |
| `GET /api/tasks/<task_id>` | – | `{status, progress, result}` for polling |
| `GET /api/clusters?brand_id=` | – | clusters with per-rewrite `{query, region, source, covered, post_numbers}` + `cluster_size/covered_count/gap_count/complete/backfilled` |
| `POST /api/clusters/backfill` | `{brand_id}` | reconstructs clusters from posts |
| `POST /api/clusters/add-posts` | `{brand_id, seed, post_numbers[]}` | attach existing prompts to a cluster |
| `POST /api/clusters/add-fanout` | `{brand_id, seed, observed_queries[]}` | merge pasted observed queries (region-deduped) |
| `POST /api/clusters/delete` | `{brand_id, seed}` | delete a cluster |

**Async task system:** `POST /api/prompts/generate` spawns a background thread, registers it in
an in-process dict keyed by `task_id`, and returns immediately. The UI polls
`GET /api/tasks/<task_id>` until `status == "done"`, then reloads. (Generation makes several
Claude calls and can take 30–90s — never block the request.)

---

## 6. Module 3 — UI (follow the client dashboard)

Match the reference portal's look. Single-page app is fine (the reference admin UI is a single
`index.html` with vanilla JS + Tailwind CDN); **no auth**.

### 6.1 Theme tokens (use verbatim)

```css
:root {
  --accent: #0d9488;      /* teal-600 */
  --accent-dark: #0f766e; /* teal-700 */
  --accent-soft: #ccfbf1; /* teal-100 */
  --bg: #f8fafc;          /* slate-50 */
  --surface: #ffffff;
  --border: #e2e8f0;      /* slate-200 */
  --text: #0f172a;        /* slate-900 */
  --text-muted: #64748b;  /* slate-500 */
}
/* Inter font; stat cards: white, 1px var(--border), radius 12px, padding 24px;
   buttons: .btn-primary = teal bg/white text/radius 8px, .btn-ghost = bordered;
   tables: .report-table with uppercase 12px muted headers, row hover #f8fafc;
   chips: .chip-live #ecfdf5/#047857, .chip-removed #fef3c7/#92400e,
          .chip-brand #eff6ff/#1d4ed8, .chip-sub #f1f5f9/#475569 */
```
Load Inter from Google Fonts; Tailwind via CDN is acceptable for a local tool.

### 6.2 Pages

| Page | Mirrors | Content |
|---|---|---|
| **Brands grid** | portal dashboard "By Brand" | card per brand (name, category, enriched ✓, # prompts); **+ Add brand**. |
| **Brand detail** | portal brand page | brand header + **Edit/Enrich**; the **ICP editor**; a **Generate prompts** panel (seed, count, intent split, AI-search on, optional "paste observed queries"); and the **Clusters** view. |
| **Cluster view** | the reference "Clusters" tab | per cluster: anchor + seed + exact `X/N` coverage; rewrite rows grouped by region with source markers (★ fixed · ⚙ generated · ✎ manual), a **covered** chip + linked prompt numbers, and a **checkbox per row**. Buttons: **Fill gaps**, **Generate for selected (N)**, **Add observed queries**, **Add prompts by #**, **Delete cluster**. |
| **Generated prompts** | portal `month.html` table | `.report-table` of prompts: # · Intent (chip) · Region · Seed · Title · Body (truncated) · Score; filters (brand / intent) + **Export CSV**. |

Map of old → new: portal *months/check-live/status chips* are dropped; their **table +
card + chip + filter patterns** are reused for the **prompts** and **clusters** views.

---

## 7. Data model reference (consolidated, trimmed schema)

```sql
-- brands (no subreddit requirement; ICPs added)
CREATE TABLE brands (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE, domain_url TEXT, context TEXT NOT NULL,
  keywords TEXT, category TEXT, audience TEXT, use_cases TEXT, pain_points TEXT,
  features TEXT, competitors TEXT, icps TEXT, target_subreddits TEXT,
  enriched_at TEXT, added_at TEXT DEFAULT (datetime('now'))
);

-- generated prompts
CREATE TABLE posts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, brand_id INTEGER REFERENCES brands(id),
  title TEXT NOT NULL, body TEXT NOT NULL, intent TEXT, ai_query_score INTEGER DEFAULT 0,
  ai_search_meta TEXT, concept_checklist TEXT, post_number INTEGER,
  prompt_version TEXT, target_subreddit TEXT, created_at TEXT DEFAULT (datetime('now'))
);

-- persisted fan-out clusters
CREATE TABLE ai_search_clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT, brand_id INTEGER, seed_norm TEXT NOT NULL,
  seed TEXT, anchor TEXT, rewrites_json TEXT, checklist_json TEXT,
  backfilled INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')),
  UNIQUE(brand_id, seed_norm)
);
```
JSON columns: `keywords/use_cases/pain_points/features/competitors/target_subreddits` =
`[str]`; `icps` = `[{label,role,segment,context,pains[]}]`; `rewrites_json` =
`[{query,region,source}]`; `checklist_json` = `[str]`; `ai_search_meta` =
`{seed,anchor,target_query}`. Always read rewrites through `normalize_rewrites` (legacy-safe).

---

## 8. LLM & config

```python
# config.py
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEFAULT_MODEL     = "claude-sonnet-4-20250514"
PROMPT_VERSION    = "v1.0"

POST_BATCH_SIZES  = (3, 6, 9)
INTENT_TYPES      = ("commercial", "comparison", "informational")
USER_AGENT        = "BotricPromptGen/1.0"

# Optional embedding relevance gate (off unless a key is set)
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "")
EMBED_MODEL       = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
EMBED_THRESHOLD   = float(os.environ.get("EMBED_THRESHOLD", "0.68"))
```

`.env` template:
```
ANTHROPIC_API_KEY=sk-ant-...
# optional:
OPENAI_API_KEY=
EMBED_MODEL=text-embedding-3-small
EMBED_THRESHOLD=0.68
```

**ClaudeClient** (`generators/base.py`): a thin wrapper around the Anthropic SDK with a
`.call(prompt, max_tokens, temperature)` method that returns parsed JSON (strip markdown
fences, `json.loads`, return `{}`/`None` on failure). All prompts above expect JSON-only
replies; keep a tolerant parser.

---

## 9. Build & run

```
pip install flask anthropic requests          # requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python app.py                                  # http://127.0.0.1:5000
```
- SQLite created on first run; enable **WAL** (`PRAGMA journal_mode=WAL`) so the background
  generation thread and the UI can read concurrently.
- No login, single operator, local only.

---

## 10. Build order & caveats

**Recommended sequence:**
1. **Schema** (§7) + `db.py` access methods + `normalize_seed`/`normalize_rewrites`.
2. **Enrichment** (§4.2–4.3): fetch → extract → Claude prompt → draft (incl. ICPs).
3. **Brand UI** (§4.4): add/edit form, ✨Enrich, ICP editor.
4. **Fan-out + clusters** (§5.2–5.4): `_fanout_rewrites`, `_dedup_cap_regions`,
   `_classify_regions`, `_merge_observed`, cluster persistence.
5. **Generation** (§5.5–5.6): candidate gen, scoring, coverage gate, optional embedding gate,
   async task.
6. **Cluster UI** (§6.2): regions, coverage, gap-fill, generate-selected, paste observed, delete.
7. **Prompts view + CSV export** (§6.2).
8. **(Optional) embedding gate** — wire last; it stays off until `OPENAI_API_KEY` is set.

**Caveats / gotchas (all addressed above):**
- **Enrich before generate.** Generation quality depends on the GEO fields; front-load
  enrichment + review in the flow.
- **Seed is optional** (matches the bot). `seed` defaults to `None`. With no seed, AI-Search
  fan-out derives the anchor from the brand's most central use-case and still builds a full
  cluster; the standard (`ai_search=False`) path uses no seed at all. A seed only *focuses* the
  fan-out around a specific prompt/platform. (Nice-to-have: a "suggest seeds" helper that
  proposes seeds from `category` + `use_cases`.)
- **Output sink.** Since nothing is posted, prompts must be **viewable + CSV-exportable** — that
  view is the product's deliverable.
- **No auth / no clients.** Drop the reference's `clients`/`client_brands`/portal-login entirely;
  keep only the visual theme.
- **Subreddit is optional.** The reference threads a subreddit through `generate_posts`; here
  it's just an optional `target_subreddits` flavor field, never required.
- **ICPs are storage-only.** They're detected, stored, editable, and folded into the
  brand-context block — but they do **not** add fan-out regions.
- **Embedding gate is graceful.** Without an OpenAI key it no-ops; never let it block generation.
- **`_select_cluster_best` never pads.** Prefer fewer distinct strong prompts over duplicates;
  log the shortfall.

---

*End of spec.*
