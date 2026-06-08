# Botric — Prompt Generation Update: First-Time Coverage Map + Auto-Enrich

**Build specification (standalone) — second deliverable**

This document specifies a self-contained update to the **standard (non-AI) prompt-generation
route**. It adds two behaviors and can be implemented on its own. Stack is unchanged (Python
Flask + SQLite + Anthropic Claude). **No database schema change, no new endpoint, no UI change.**

The two behaviors:
1. **Auto-enrich on generate** — when a brand has no enrichment yet, fill its GEO fields at
   generate-time, **never overwriting the operator's manual `context`**.
2. **First-time / no-seed coverage map** — when generating for a brand with **no seed**, derive a
   deliberate coverage map from the brand's enrichment so the batch spans **distinct offerings
   (one per post)** instead of clustering on one.

---

## 0. Why

On the standard route with no seed, generation was open-ended: every per-intent call got the same
brand context plus one *soft* "cover all offerings" line, with no per-post target. Two problems:

- **Clustering.** A 6-post batch often piled onto the brand's flagship feature; nothing told the
  model *which* use-case / pain-point / competitor each post should hit.
- **Un-enriched brands.** Most brands are added with a hand-written `context` and **no** structured
  enrichment (`use_cases` / `pain_points` / `competitors`), so there was nothing to spread across.

The fix turns the brand's **own enrichment into the coverage space** ("the brand is the seed"), and
self-heals enrichment first so even a manually-added brand has the data — without ever touching the
operator's manual `context`.

---

## 1. Behavior A — Auto-enrich on generate (preserve manual `context`)

At the start of the generation job, if the brand isn't enriched yet, enrich its **GEO fields only**
(`category`, `audience`, `use_cases`, `pain_points`, `features`, `competitors`) and stamp
`enriched_at`. The operator's manual `context` is **never written** (the enrichment draft's
`context_summary` is discarded). This is a one-time, per-brand Claude call inside the background
generation task, and it benefits **both** routes (the AI fan-out reads the same enriched block).

### 1.1 "Needs enrichment" test
A brand needs enrichment when it has **no `enriched_at`** AND **no GEO field** populated. A brand the
operator partly filled by hand (any GEO field present) is left alone.

```python
def _brand_needs_enrichment(brand):
    """True when a brand has no enrichment yet — no `enriched_at` AND no GEO fields.
    Brands the user partly filled by hand (any GEO field present) are left alone, and
    the manual `context` is never touched by enrichment."""
    if not brand:
        return False
    if (brand.get("enriched_at") or "").strip():
        return False
    def _has(v):
        return bool(v) and str(v).strip() not in ("", "[]", "null", "{}")
    return not any(_has(brand.get(k)) for k in
                   ("category", "use_cases", "pain_points", "competitors"))
```

### 1.2 Enrich step (inside the generate job, right after loading the brand)
Reuse the existing enrichment function (it works even with no website URL — Claude falls back to the
brand name + general knowledge) and the existing brand-update method that **only writes the fields
you pass** (so omitting `context` preserves it):

```python
brand_full = db.get_brand(bid)
if not brand_full:
    raise ValueError("Brand vanished")

# Self-heal: a brand added with only a MANUAL context (not enriched) has no
# use_cases/pain_points/competitors, so first-time coverage + the AI fan-out
# have nothing to work with. Enrich the GEO fields now — but NEVER overwrite
# the user's manual `context` (we discard the draft's context_summary).
if _brand_needs_enrichment(brand_full):
    try:
        from generators.brand_enrichment import enrich_brand
        from datetime import datetime as _dt_now
        draft = enrich_brand(claude, brand_full.get("name") or "",
                             brand_full.get("domain_url") or "")
        if draft:
            db.update_brand(
                bid,
                category=(draft.get("category") or None),
                audience=(draft.get("audience") or None),
                use_cases=json.dumps(draft.get("use_cases") or []),
                pain_points=json.dumps(draft.get("pain_points") or []),
                features=json.dumps(draft.get("features") or []),
                competitors=json.dumps(draft.get("competitors") or []),
                enriched_at=_dt_now.now().strftime("%Y-%m-%d %H:%M:%S"),
            )  # context intentionally omitted → manual context preserved
            brand_full = db.get_brand(bid)
            print(f"[live-posts] auto-enriched brand {bid} on generate (manual context kept)")
    except Exception as e:
        print(f"[live-posts] auto-enrich skipped for brand {bid}: {e}")
```

Rules:
- **`context` is never passed to `update_brand`** → the manual context is untouched. `context_summary`
  from the enrichment draft is intentionally dropped.
- `category`/`audience` are written only when non-empty (pass `None` to skip); list fields are stored
  as JSON strings.
- **Graceful:** any enrichment failure is caught and logged; generation proceeds un-enriched (it then
  simply falls back to open-ended generation — see §2 fallbacks).
- `enriched_at` is set even if the model returned sparse fields, so generation never re-calls the
  enrichment API on every run.

> The brand-update method must follow the "update only the params you pass" pattern (build the
> `SET` clause only from non-`None` arguments) so a single call can write GEO fields while leaving
> `context`, `keywords`, `focus`, etc. exactly as they were.

---

## 2. Behavior B — First-time / no-seed coverage map (facets)

When generating with **no seed**, build a coverage map from the brand's enrichment: each individual
`use_case`, `competitor`, and `pain_point` becomes a **facet**, grouped by the intent it naturally
fits. Then hand **one distinct facet per post** to generation, so the batch deliberately spans the
brand instead of clustering. This is *not* the AI-Search cluster — **no fan-out, no persistence,
per-run only**.

### 2.1 Facet → intent mapping

| Enrichment field | Intent | Facet form |
|---|---|---|
| `use_cases` | **commercial** | the use-case phrase as-is |
| `competitors` | **comparison** | `"alternative to <competitor>"` |
| `pain_points` | **informational** | the pain-point phrase as-is |

### 2.2 Facet builder

```python
@staticmethod
def _brand_facets(brands):
    """First-time / no-seed coverage map: turn a brand's enrichment into a flat,
    de-duped list of concrete FACETS grouped by the intent that fits each:
      use_cases   -> commercial    (the jobs people buy the product to do)
      competitors -> comparison    ("alternative to <competitor>")
      pain_points -> informational (the problems people research)
    Returns {"commercial": [...], "comparison": [...], "informational": [...]}.
    Empty lists when the brand isn't enriched — callers then fall back to the
    normal open-ended generation."""
    def _plist(val):
        if isinstance(val, list):
            return [str(x).strip() for x in val if str(x).strip()]
        if isinstance(val, str) and val.strip():
            try:
                p = json.loads(val)
                if isinstance(p, list):
                    return [str(x).strip() for x in p if str(x).strip()]
            except (json.JSONDecodeError, TypeError):
                pass
            return [s.strip() for s in val.split(",") if s.strip()]
        return []
    blist = brands if isinstance(brands, list) else [brands]
    out = {"commercial": [], "comparison": [], "informational": []}
    seen = set()
    def _add(intent, label):
        label = (label or "").strip()
        k = label.lower()
        if label and k not in seen:
            seen.add(k)
            out[intent].append(label)
    for b in blist:
        for uc in _plist(b.get("use_cases")):
            _add("commercial", uc)
        for cp in _plist(b.get("competitors")):
            _add("comparison", f"alternative to {cp}")
        for pp in _plist(b.get("pain_points")):
            _add("informational", pp)
    return out
```

### 2.3 Gating + per-post allocation (in the generation entrypoint)

Facets are active **only** when generating without a seed on the standard route:

```python
# First-time / no-seed standard route: derive a coverage map from the brand's
# enrichment so the batch deliberately spans distinct offerings (one facet per
# post) instead of clustering. Off for AI-Search (it has its own cluster), and
# when a seed or custom_topics already direct the batch. Empty when un-enriched
# → graceful fallback to today's open-ended generation.
facets_by_intent = None
if (not ai_search) and not (seed and str(seed).strip()) and not custom_topics:
    _fm = self._brand_facets(brands)
    if any(_fm.values()):
        facets_by_intent = _fm
```

Inside the per-intent loop, take up to `n_intent` facets for that intent and **consume** them so no
facet repeats across the batch. In facet mode, request exactly `n_intent` candidates (**no
oversampling**) — oversample-then-score-select could drop a facet to double up another, defeating the
spread:

```python
# First-time coverage: hand this intent up to n_intent distinct facets
# (consumed so no facet repeats across the batch). No oversampling in
# facet mode — oversample + score-select could drop a facet to double
# up another, defeating the spread.
facet_targets = None
req_count = n_intent * 2
if facets_by_intent is not None:
    _avail = facets_by_intent.get(intent) or []
    if _avail:
        facet_targets = _avail[:n_intent]
        facets_by_intent[intent] = _avail[len(facet_targets):]
        req_count = n_intent
candidates = self._generate_candidates_for_intent(
    subreddit, brands, intent, storylines_for_intent,
    existing_titles, req_count, context_only=context_only,
    seed_focus=(None if ai_search else seed),
    coverage_focus=intent_focus,
    facet_targets=facet_targets,
)
```

Selection for the standard route is unchanged (pick the best `n_intent` with storyline variety); the
existing within-batch title dedup still applies.

### 2.4 The `COVERAGE TARGETS` prompt block

The per-intent candidate generator accepts `facet_targets` and injects this block — **only** when no
AI-Search coverage block and no seed block are present. It instructs one distinct post per facet and
asks the model to report which facet each title covered:

```python
facet_block = ""
if facet_targets and not coverage_focus and not (seed_focus and str(seed_focus).strip()):
    _ft = [str(f).strip() for f in facet_targets if str(f).strip()]
    if _ft:
        ft_lines = "\n".join(f"  - {f}" for f in _ft)
        _extra = count - len(_ft)
        extra_rule = (
            f"  • For the remaining {_extra} post(s) beyond these targets, pick OTHER "
            "distinct brand offerings/angles — never repeat a target or an offering "
            "already used in this batch.\n"
        ) if _extra > 0 else ""
        facet_block = (
            "\n\nCOVERAGE TARGETS — to give this batch deliberate spread, cover the "
            "brand's DISTINCT offerings below, ONE per post (no two posts on the same one):\n"
            f"{ft_lines}\n"
            "  • Each title TARGETS exactly ONE item above and must be a natural "
            "RECOMMENDATION QUESTION for it (a helpful AI would answer by naming a "
            "product/service); different posts target DIFFERENT items.\n"
            f"{extra_rule}"
            "  • These are the SPACE to cover, NOT templates — phrase each as a real "
            "human question (all TITLE rules below still apply); never paste an item verbatim.\n"
            "  • Report which item each title targets in its \"target_query\" field.\n"
        )
        coverage_json_field = ',\n            "target_query": "the ONE coverage target this title covers"'
```

The block is concatenated into the prompt header alongside the (mutually exclusive) seed and
AI-Search coverage blocks: `header = f"""{scope_line}{seed_block}{coverage_block}{facet_block}..."""`.

### 2.5 Graceful fallbacks (all required)
- **Un-enriched / empty enrichment** → `_brand_facets` returns all-empty → `facets_by_intent` stays
  `None` → behaves exactly like open-ended generation today (with the existing soft "cover all
  offerings" line as backstop).
- **More posts than facets for an intent** → cover the facets one-each, and the `extra_rule` tells
  the model to fill the remaining posts with *other distinct* offerings.
- **Fewer requested than facets** → only the first `n_intent` facets are used this run (per-run only;
  no memory — the next run is independent).

---

## 3. Precedence (one place)

Exactly one steering mode drives a given generation call, in this order:

| Condition | Mode |
|---|---|
| `ai_search = true` | AI-Search region cluster (fan-out + `coverage_focus`) — unchanged |
| `seed` present (and not AI-Search) | Seed focus — unchanged |
| `custom_topics` present | Custom titles appended — unchanged |
| none of the above, brand has facets | **First-time COVERAGE TARGETS (this update)** |
| none of the above, no facets | Plain open-ended (existing "cover all offerings" backstop) |

The `COVERAGE TARGETS` block is suppressed whenever a seed block or AI-Search coverage block is
present, so the modes never collide.

---

## 4. Scope of change

- **No DB schema change.** Reuses existing brand columns + the existing brand-update method.
- **No new endpoint.** The change lives in the generation entrypoint (auto-enrich) and the generator
  (facets).
- **No UI change.** The Generate action already sends no seed for first-time generation.
- `target_query` is now also emitted for standard facet posts (used only to label which offering a
  post covered; persist it only if your posts table already stores it — otherwise ignore on save).

---

## 5. Build order

1. Add `_brand_facets` (static helper on the generator).
2. Gate + consume facets in the generation entrypoint (per-intent loop), passing `facet_targets`.
3. Add the `facet_targets` parameter + `COVERAGE TARGETS` block to the per-intent candidate generator.
4. Add `_brand_needs_enrichment` + the auto-enrich step at the top of the generate job (preserving
   `context`).

## 6. Verification

- **`_brand_facets`**: a brand with 3 use_cases / 2 competitors / 3 pain_points → facets grouped to
  the right intents, de-duped, competitors rendered as `"alternative to <X>"`. Empty enrichment →
  all-empty.
- **Prompt injection**: with `intent_counts = {commercial:2, comparison:1, informational:2}`, the
  commercial prompt carries 2 distinct use-case targets, comparison 1 competitor target,
  informational 2 pain-point targets; the `COVERAGE TARGETS` header is present; no facet repeats
  across the batch.
- **Suppression**: a `seed` present → no `COVERAGE TARGETS` (seed block used); `ai_search = true` →
  no `COVERAGE TARGETS` (AI-Search coverage used).
- **Extra-fill**: request more posts than facets for an intent → the "remaining N post(s)" rule
  appears.
- **Auto-enrich**: un-enriched brand (manual `context`, `enriched_at` null, empty GEO) → after a
  generate call, GEO fields populated + `enriched_at` set AND `context` byte-for-byte unchanged.
  Already-enriched or manually-filled brand → `_brand_needs_enrichment` is False (no enrich call).
  Enrichment failure → generation still proceeds.

---

*End of update spec.*
