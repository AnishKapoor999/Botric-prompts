"""The fan-out -> cluster -> generate pipeline (the product).

Implements the AI-Search region/sparse-fan-out -> cluster -> generate algorithm exactly
as specified: derive an anchor, instantiate distinct REGIONS, generate ONE strong prompt
per region x intent, score (anchor- and recommendation-aware), coverage-gate, and
(optional) embedding-gate.
"""
import json
import math

import requests

import db
from config import (
    EMBED_MODEL,
    EMBED_THRESHOLD,
    INTENT_TYPES,
    MAX_FANOUT_REWRITES,
    OPENAI_API_KEY,
    POST_BATCH_SIZES,
    PROMPT_VERSION,
)
from generators.base import BANNED_PHRASES, ClaudeClient

# Quality floor for the standard (non-AI) route: drop candidates scoring below this
# rather than padding the batch with weak/vent titles. (AI-Search uses threshold 6 in
# _select_cluster_best.)
_STANDARD_SCORE_FLOOR = 5


# ===========================================================================
# Prompt templates
# ===========================================================================

_FANOUT_PROMPT = """You are mapping the AI-search query space for a GEO campaign. The goal is to get
this brand recommended by AI assistants. AI engines REWRITE a user's prompt into
several search sub-queries ("query fan-out") and retrieve SEMANTICALLY, so we need
the full cluster of phrasings around the brand's buying/recommendation intent —
NOT one literal phrasing.

BRAND CONTEXT (ground the queries here; NEVER output the target brand name(s): {target_names}):
{brand_block}
{seed_line}{grounding_line}{persona_lens}
Do this in your head, then return the merged result:
  1. ANCHOR — identify the core platform / use-case this campaign targets. If the
     seed names a platform or use-case (e.g. "Instagram Reels", "podcast intros",
     "online store checkout"), THAT is the anchor — extract it exactly. If there's
     no seed, derive the anchor from the brand's single most central use-case/
     platform. The anchor is short (1-4 words). Every rewrite stays on this anchor;
     a rewrite may broaden to a NAMED adjacent variant (e.g. Reels -> Shorts /
     short-form) but never collapses to something generic ("my videos", "content").
  2. REGIONS — real AI engines fan a prompt into only a FEW sub-queries (ChatGPT
     ~2-3, Perplexity ~3-6, Gemini ~3-5), and across engines they converge on the
     same handful of DISTINCT regions, NOT a dozen synonyms. Produce ONE rewrite per
     region. Consider these 6 standard axes and instantiate the ones that apply to
     THIS brand/seed (SKIP any that don't fit); fill the "Constraint" slot with this
     brand's dominant buying constraint (copyright/licensing, price/free, compliance,
     integration, speed...):
       - Category / leading option -> the leading option in the category for their need (phrase naturally; do NOT default to a "best …" wording)
       - Comparison / alternative  -> "X vs Y", "alternative to <competitor>"
       - Constraint                -> the #1 buying concern (e.g. copyright-safe, budget)
       - Use-case / workflow        -> the specific job (auto-sync to video, for podcasts)
       - Persona / segment          -> for small business, for beginners/pros
       - Adjacent platform          -> a NAMED adjacent (Reels -> Shorts / TikTok)
     You MAY add up to 2 brand-specific regions the 6 don't capture.
  2b. ENTITY-TYPE — every rewrite must be a query an AI would answer by naming a brand
     of THIS brand's KIND (see its "Category"); keep every region within that kind. If
     the brand is a SERVICE / PROVIDER / CLINIC, regions are provider-oriented (best
     service/clinic, where to get it / who offers it, alternative to a competitor
     PROVIDER) — NOT treatment-vs-treatment or efficacy ("which works better / does X
     work") comparisons. (For product / retailer brands this is already satisfied —
     keep the usual product / buy-intent regions.)
  3. Write ONE rewrite per region — phrased the way the engines actually search
     (short, keyword-ish, real intent), distinct from the others. VARY the phrasing
     across regions; do NOT force a "best …" shape on every one. HARD RULE: if two
     rewrites would get essentially the SAME AI answer, keep only ONE. Aim for
     ~5-8 rewrites TOTAL — fewer, distinct regions beat many synonyms.
  4. Produce a concise CONCEPT/PHRASING CHECKLIST: the key natural-language
     phrasings, synonyms and domain terms a Reddit thread should contain so it
     matches the whole cluster for both keyword (BM25) and embedding retrieval.

Return JSON only:
{{
  "anchor": "the platform/use-case to keep every title on (short)",
  "rewrites": [
    {{"query": "the sub-query, engine-style", "region": "one of the 6 axis names OR a brand-specific region", "fixed": true/false (true ONLY if it's one of the 6 standard axes)}},
    ...
  ],
  "checklist": ["phrasing/term 1", "phrasing/term 2", "..."]
}}
Aim for 5-8 rewrites total. Never include the target brand name."""


_SEED_LINE = """ANCHOR SEED (expand the fan-out AROUND this — an existing prompt/question, several, or a keyword/platform):
{seed}"""

_COVERED_BLOCK = """ALREADY COVERED — these sub-queries are already handled by existing threads. Produce
DISTINCT NEW rewrites for the REMAINING space (other sub-use-cases / buyer concerns /
phrasings). Do NOT repeat or lightly reword any of these:
{covered_lines}"""


_CLASSIFY_PROMPT = """You are classifying observed AI-search sub-queries into REGIONS for a GEO campaign.
A "region" is a distinct buyer-concern angle (e.g. "Category / best tool",
"Comparison / alternative", "Constraint", a persona, an adjacent platform...).

EXISTING REGIONS (reuse a label VERBATIM when a query fits it; otherwise name a new
concise 2-4 word region):
{existing_regions}

QUERIES TO CLASSIFY:
{queries}

Return JSON only:
{{"classified": [{{"query": "the query verbatim", "region": "region label"}}, ...]}}"""


_CANDIDATE_PROMPT = """You are writing ONE Reddit post for a GEO campaign. The goal: when a real person
asks an AI assistant (ChatGPT / Perplexity / Gemini) the kind of question below, this
thread is what gets retrieved and the answer recommends the right product. You must
NOT name any of these brands: {target_names}.

BRAND CONTEXT (for grounding only — never name the brand):
{brand_block}

ANCHOR (every title MUST stay on this platform/use-case; you may broaden ONLY to a
NAMED adjacent variant, never to something generic like "my videos"): {anchor}

INTENT: {intent}
  - commercial: someone looking to choose/buy a tool — its natural answer names a specific product to use.
  - comparison: weighing named options ("X vs Y", "alternative to <competitor>").
  - informational: advice-seeking that should still surface a product recommendation.

TARGET SUB-QUERY (the engine-style query this thread must land on): "{target_query}"

CONCEPT / PHRASING CHECKLIST (weave these natural phrasings, synonyms and domain
terms into the BODY so the thread matches for both keyword (BM25) and embedding
retrieval):
{checklist}
{coverage_block}
WRITE THE POST so that:
  - The TITLE is ALWAYS a recommendation QUESTION — never a vent, testimonial, rant, or
    status update ("so tired of X", "X changed my life"). It reads like a real person
    asking what to use/buy/try (not a bare statement, not machine-speak). On-anchor. A
    verbatim copy of the engine query looks like spam — paraphrase it naturally. VARY the
    structure/opening — do NOT default to a "best …" shape; phrase it as a real person
    would actually ask.
  - OUR-BRAND CHECK — the TITLE's natural answer must name the SAME KIND of entity as
    this brand (see its "Category" in BRAND CONTEXT above):
      - PRODUCT / TOOL / APP → ask which product/tool to use for the use-case (its natural
        answer names a specific product); product-vs-product comparisons are fine.
      - SUPPLIER / RETAILER / MARKETPLACE → ask where to buy / who sells / the best place
        to order the thing.
      - SERVICE / PROVIDER / CLINIC (you go to it to GET something done) → ask for the
        PROVIDER: the best service / clinic / where to get it / who offers it for the
        use-case. Do NOT frame it as "best <treatments>", "<A> vs <B> — which works
        better?", or "does <X> work?" (those are answered with substances / efficacy,
        not a provider, so this brand can't be the cited answer). A comparison is fine
        only when it compares PROVIDERS, e.g. "alternative to <competitor clinic>".
  - The BODY (2-5 short paragraphs) sounds like a genuine poster giving context, and it
    CONTAINS the target sub-query's literal wording (for keyword match) alongside natural
    paraphrases and the checklist phrasings (for embedding match). End with a real
    question.
  - Avoid marketing/AI-tell phrases such as: {banned}.

Return JSON only:
{{"title": "the post title", "body": "the post body"}}"""


_SCORE_PROMPT = """You are scoring a Reddit post TITLE for a GEO campaign whose goal is to get a
specific product/service recommended by AI assistants (ChatGPT / Perplexity / Google).

Title: "{title}"
Body preview: "{body_preview}"

Rate 0-10 on BOTH dimensions together — judge by THE ANSWER THE TITLE WOULD GET, not its wording:
  (1) How likely a real person types this exact question (or close paraphrase) into an AI / search engine, AND
  (2) Whether the NATURAL ANSWER is to RECOMMEND a specific product / brand / service to use or buy.

High (8-10): Clearly recommendation-seeking — a helpful AI would answer by naming specific products/services/suppliers. ANY phrasing qualifies as long as the natural answer is a specific recommendation (which-to-use, where-to-buy, who-sells, alternative-to, go-to). Do NOT favor a "best …" wording — varied phrasings score just as high.
Medium (5-7): Advice-seeking that MIGHT surface a product recommendation ("has anyone tried X", "what do you use for Y").
Low (1-4): Generic information / efficacy / how-it-works / "what to look for" / concept questions where the answer is an EXPLANATION rather than a product recommendation (e.g. "do X actually work", "how does X work", "what is X"); also rants, memes, very personal one-offs.
CRITICAL: first-person VENTS, TESTIMONIALS and STATUS UPDATES that don't ASK for anything are NOT recommendation-seeking — score them 1-4 even if on-topic (e.g. "frustrated with traditional doctors dismissing X", "so tired of Y", "started using Z — game changer", "X changed my life", "finally found something that works"). A title only scores high if it explicitly asks for what to use/buy/try.
{ai_search_block}{entity_block}
Return JSON only:
{{"score": 0-10, "reasoning": "brief explanation"}}"""


_ENTITY_TYPE_BLOCK = """
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
"""


_AI_SEARCH_SCORE_BLOCK = """
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
"""


_PERSONA_ASSIGN_PROMPT = """You are assigning buyer PERSONAS to search queries for a GEO campaign. For EACH query
below, rank the personas from MOST to LEAST likely to be the person who would type that
exact query — judge by their goal, trigger, and vocabulary.

PERSONAS:
{persona_block}

QUERIES (in order):
{query_block}

Rank ALL personas for every query (a COMPLETE ranking, most-likely first). Do NOT return
empty lists and do NOT omit any persona — even a weak fit goes at the END of that query's
ranking. Use ONLY the persona labels exactly as written above.

Return JSON only, with one ranked list per query IN THE SAME ORDER as the queries above:
{{"assignments": [["label", "label", ...], ...]}}"""


class PostGenerator:
    """Encapsulates the generation pipeline. Stateless except for the Claude client."""

    def __init__(self, client=None):
        self.client = client or ClaudeClient()

    # -------------------------------------------------------------------
    # Brand context block
    # -------------------------------------------------------------------
    @staticmethod
    def _target_names(brands):
        names = []
        for b in brands:
            n = (b.get("name") or "").strip()
            if n:
                names.append(n)
        return ", ".join(names) if names else "(none)"

    # -------------------------------------------------------------------
    # First-time helpers: auto-enrich gate + facet coverage map
    # -------------------------------------------------------------------
    @staticmethod
    def _brand_needs_enrichment(brand):
        """True when a brand has no enrichment yet — no `enriched_at` AND no GEO fields.

        Brands the user partly filled by hand (any GEO field present) are left alone,
        and the manual `context` is never touched by enrichment.
        """
        if not brand:
            return False
        if (brand.get("enriched_at") or "").strip():
            return False

        def _has(v):
            if v is None:
                return False
            if isinstance(v, (list, dict)):
                return len(v) > 0
            return str(v).strip() not in ("", "[]", "null", "{}")

        return not any(_has(brand.get(k)) for k in
                       ("category", "use_cases", "pain_points", "competitors"))

    @staticmethod
    def _brand_facets(brands):
        """First-time / no-seed coverage map: turn a brand's enrichment into a flat,
        de-duped list of concrete FACETS grouped by the intent that fits each:
          use_cases   -> commercial    (the jobs people buy the product to do)
          competitors -> comparison    ("alternative to <competitor>")
          pain_points -> informational (the problems people research)
        Returns {"commercial": [...], "comparison": [...], "informational": [...]}.
        Empty lists when the brand isn't enriched — callers then fall back to the
        normal open-ended generation.
        """
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

    @staticmethod
    def _build_enriched_brand_block(brands):
        """Compact, human-readable brand-context block read by generation.

        ICPs are folded in loosely as a single summary line, exactly as `audience` is —
        they do NOT create new fan-out regions.
        """
        lines = []
        for b in brands:
            lines.append(f"BRAND: {b.get('name', '')}")
            if b.get("context"):
                lines.append(f"  Context: {b['context']}")
            if b.get("category"):
                lines.append(f"  Category: {b['category']}")
            if b.get("audience"):
                lines.append(f"  Audience: {b['audience']}")

            icps = b.get("icps") or []
            if icps:
                labels = []
                for icp in icps:
                    if isinstance(icp, dict):
                        lbl = (icp.get("label") or icp.get("role") or "").strip()
                        if lbl:
                            labels.append(lbl)
                if labels:
                    lines.append("  ICPs: " + "; ".join(labels))

            for field, label in (
                ("use_cases", "Use cases"),
                ("pain_points", "Pain points"),
                ("features", "Features"),
                ("competitors", "Competitors"),
                ("keywords", "Keywords"),
            ):
                vals = b.get(field) or []
                if vals:
                    lines.append(f"  {label}: {', '.join(str(v) for v in vals)}")

            # Anchor-scoped grounding: what the brand actually offers for specific topics
            # (only entries where covers=true; learned per seed and accumulated).
            learned = b.get("learned_context") or {}
            if isinstance(learned, dict):
                offers = []
                for entry in learned.values():
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("covers") and (entry.get("summary") or "").strip():
                        anc = (entry.get("anchor") or "").strip()
                        summ = entry["summary"].strip()
                        offers.append(f"    - {anc}: {summ}" if anc else f"    - {summ}")
                if offers:
                    lines.append("  What the brand offers for specific topics (learned):")
                    lines.extend(offers)
        return "\n".join(lines)

    # -------------------------------------------------------------------
    # Buyer personas (brand-level, lazy + cached)
    # -------------------------------------------------------------------
    def _ensure_personas(self, brands):
        """Lazily generate + cache brand-level personas (one Claude call, one-time).

        Gate is the `personas` field (not enriched_at) -> existing brands auto-fill on
        their next AI generate. Mutates the in-memory brand; never touches `context`.
        """
        if not brands:
            return
        brand = brands[0]
        if brand.get("personas"):
            return  # cached
        bid = brand.get("id")
        from generators.brand_enrichment import generate_brand_personas
        personas = generate_brand_personas(
            brand.get("name") or "", brand.get("domain_url") or "",
            category=brand.get("category"), audience=brand.get("audience"),
            use_cases=brand.get("use_cases"), pain_points=brand.get("pain_points"),
            client=self.client)
        if not personas:
            return
        brand["personas"] = personas              # mutate in-memory for this run
        if bid is not None:
            try:
                db.update_brand(bid, {"personas": json.dumps(personas)})
            except Exception as e:
                print(f"[personas] persist failed for brand {bid}: {e}")

    @staticmethod
    def _fit_personas(brands):
        """Personas with a label and fit in {yes, maybe} (drops fit:no — winnability)."""
        if not brands:
            return []
        out = []
        for p in (brands[0].get("personas") or []):
            if (isinstance(p, dict) and (p.get("label") or "").strip()
                    and p.get("fit") in ("yes", "maybe")):
                out.append(p)
        return out

    def _build_persona_lens(self, brands):
        """Fan-out LENS block from the winnable personas. '' when there are none."""
        fit = self._fit_personas(brands)
        if not fit:
            return ""
        lines = ["\nPERSONAS — the real askers for this brand (use as a LENS, NOT as "
                 "separate regions):"]
        for p in fit:
            label = (p.get("label") or "").strip()
            profile = (p.get("profile") or "").strip()
            goal = (p.get("goal") or "").strip()
            vocab = (p.get("vocab") or "").strip()
            bits = f"  - {label}: {profile}"
            if goal:
                bits += f" | wants: {goal}"
            if vocab:
                bits += f" | says it like: {vocab}"
            lines.append(bits)
        lines.append("  - Use them to GROUND the rewrites in how real askers phrase things "
                     "and to decide which questions are worth targeting; SKIP any question "
                     "no persona above would credibly bring to THIS brand.")
        lines.append("  - Do NOT create a region per persona or a persona x axis matrix — "
                     "still ONE rewrite per DISTINCT region.")
        return "\n".join(lines)

    def _assign_personas_to_regions(self, rewrites, personas):
        """Tag each rewrite['persona'] — code-guaranteed spread (Update #7).

        The LLM supplies only a preference ranking; CODE guarantees full coverage and an
        even distribution under a cap. Fit-driven (only fit yes/maybe personas), never
        forced: each region gets the best-fitting AVAILABLE persona; the cap only
        redirects to the next-best fit when one is already even-handedly used. Mutates
        rewrites in place.
        """
        if not rewrites:
            return
        fit = [p for p in (personas or [])
               if isinstance(p, dict) and (p.get("label") or "").strip()
               and p.get("fit") in ("yes", "maybe")]
        # 0 fit -> no tagging (no LLM call); 1 fit -> it owns every region.
        if not fit:
            for r in rewrites:
                r["persona"] = ""
            return
        if len(fit) == 1:
            only = fit[0]["label"].strip()
            for r in rewrites:
                r["persona"] = only
            return

        labels = [p["label"].strip() for p in fit]
        label_by_key = {l.lower(): l for l in labels}

        # One Claude call: full ranking per region (preference signal only).
        persona_block = "\n".join(
            f"  - {p['label']}: {p.get('profile', '')} | wants: {p.get('goal', '')} | "
            f"trigger: {p.get('trigger', '')} | says: {p.get('vocab', '')}" for p in fit)
        query_block = "\n".join(f"  {i + 1}. {r.get('query', '')}"
                                for i, r in enumerate(rewrites))
        prompt = _PERSONA_ASSIGN_PROMPT.format(persona_block=persona_block,
                                               query_block=query_block)
        result = self.client.call(prompt, max_tokens=900, temperature=0.2)
        raw = result.get("assignments") if isinstance(result, dict) else None
        if not isinstance(raw, list):
            raw = []

        # Per-region candidates: valid LLM-ranked labels (deduped) + backfill with every
        # fit persona not yet listed (brand order). Guarantees a full fallback set.
        ranked_by_region = []
        for i in range(len(rewrites)):
            ranked, seen = [], set()
            row = raw[i] if (i < len(raw) and isinstance(raw[i], list)) else []
            for lab in row:
                real = label_by_key.get(str(lab).strip().lower())
                if real and real not in seen:
                    seen.add(real)
                    ranked.append(real)
            for l in labels:
                if l not in seen:
                    ranked.append(l)
                    seen.add(l)
            ranked_by_region.append(ranked)

        # Greedy assignment under the minimum cap that still permits full coverage.
        R, N = len(rewrites), len(labels)
        cap = max(1, math.ceil(R / N))
        counts = {l: 0 for l in labels}
        for i, r in enumerate(rewrites):
            chosen = next((c for c in ranked_by_region[i] if counts[c] < cap), None)
            if chosen is None:  # defensive (should never fire); least-used persona
                chosen = min(labels, key=lambda l: counts[l])
            counts[chosen] += 1
            r["persona"] = chosen
        print("[personas] assigned: " + ", ".join(
            f"{(r.get('region') or '?')}->{r['persona']}" for r in rewrites))

    # -------------------------------------------------------------------
    # Anchor-scoped grounding
    # -------------------------------------------------------------------
    def _ground_brand_for_anchor(self, brands, seed, seed_norm):
        """Learn what the primary brand actually offers for the seed TOPIC.

        Cached per `seed_norm` in `brands[0]['learned_context']` (no refetch on reuse),
        persisted on the brand (never touching the manual `context`), and the in-memory
        brand is mutated so the summary flows into this run's fan-out + post prompts.
        Returns the grounding summary string ('' when there's no seed or on failure).
        """
        if not seed_norm or not brands:
            return ""
        brand = brands[0]
        bid = brand.get("id")
        learned = brand.get("learned_context") or {}
        if not isinstance(learned, dict):
            learned = {}

        cached = learned.get(seed_norm)
        if isinstance(cached, dict) and (cached.get("summary") or "").strip():
            if not cached.get("covers"):
                print(f"[ground] weak fit (cached): brand {bid} seed {seed!r}")
            return cached["summary"]

        from generators.brand_enrichment import enrich_brand_for_anchor
        from datetime import datetime as _dt
        g = enrich_brand_for_anchor(brand.get("name") or "",
                                    brand.get("domain_url") or "", seed,
                                    client=self.client)
        if not g:
            return ""
        entry = {
            "anchor": (seed or "").strip(),
            "summary": g.get("summary", ""),
            "covers": bool(g.get("covers", True)),
            "key_points": g.get("key_points") or [],
            "added_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        learned[seed_norm] = entry
        brand["learned_context"] = learned            # mutate in-memory for this run
        if bid is not None:
            try:
                db.update_brand(bid, {"learned_context": json.dumps(learned)})
            except Exception as e:
                print(f"[ground] persist failed for brand {bid}: {e}")
        if not entry["covers"]:
            print(f"[ground] weak fit: brand {bid} may not serve seed topic {seed!r}")
        return entry["summary"]

    # -------------------------------------------------------------------
    # Region fan-out
    # -------------------------------------------------------------------
    def _fanout_rewrites(self, brands, seed=None, prior_coverage=None,
                         learned_summary=None):
        """One Claude call -> the cluster. Returns {anchor, rewrites, checklist} or None."""
        brand_block = self._build_enriched_brand_block(brands)
        target_names = self._target_names(brands)

        seed_line = ""
        if seed and str(seed).strip():
            seed_line = _SEED_LINE.format(seed=str(seed).strip())
        if prior_coverage:
            covered_lines = "\n".join(f"  - {q}" for q in sorted(prior_coverage))
            block = _COVERED_BLOCK.format(covered_lines=covered_lines)
            seed_line = (seed_line + "\n" + block) if seed_line else block

        grounding_line = ""
        if (learned_summary or "").strip():
            grounding_line = ("\nWHAT THIS BRAND OFFERS FOR THE SEED TOPIC (stay truthful "
                              f"to this): {learned_summary.strip()}")

        persona_lens = self._build_persona_lens(brands)

        prompt = _FANOUT_PROMPT.format(
            target_names=target_names,
            brand_block=brand_block,
            seed_line=seed_line,
            grounding_line=grounding_line,
            persona_lens=persona_lens,
        )
        result = self.client.call(prompt, max_tokens=1500, temperature=0.7)
        if not result or not isinstance(result, dict):
            return None

        anchor = str(result.get("anchor") or "").strip()
        checklist = [str(x).strip() for x in (result.get("checklist") or []) if str(x).strip()]

        covered_lower = {q.strip().lower() for q in (prior_coverage or set())}
        parsed = []
        for r in (result.get("rewrites") or []):
            if not isinstance(r, dict):
                # legacy flat string
                q = str(r).strip()
                if not q:
                    continue
                parsed.append({"query": q, "region": "(unsorted)",
                               "source": "generated", "persona": ""})
                continue
            q = (r.get("query") or "").strip()
            if not q:
                continue
            if q.lower() in covered_lower:
                continue
            region = (r.get("region") or "(unsorted)").strip() or "(unsorted)"
            source = "fixed" if r.get("fixed") else "generated"
            # persona is assigned by the dedicated code pass below, not the model.
            parsed.append({"query": q, "region": region, "source": source,
                           "persona": ""})

        if not parsed:
            return None

        rewrites = self._dedup_cap_regions(parsed)
        if not rewrites:
            return None
        # Persona tagging is a dedicated, code-enforced pass (not inline in the fan-out).
        self._assign_personas_to_regions(rewrites, (brands[0].get("personas") or []))
        return {"anchor": anchor, "rewrites": rewrites, "checklist": checklist}

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

    # -------------------------------------------------------------------
    # Observed fan-out capture
    # -------------------------------------------------------------------
    def _classify_regions(self, queries, existing_regions):
        """Map each pasted query to a region (reuse existing label when it fits)."""
        queries = [str(q).strip() for q in (queries or []) if str(q).strip()]
        if not queries:
            return []
        existing = "\n".join(f"  - {r}" for r in (existing_regions or [])) or "  (none yet)"
        q_lines = "\n".join(f"  - {q}" for q in queries)
        prompt = _CLASSIFY_PROMPT.format(existing_regions=existing, queries=q_lines)
        result = self.client.call(prompt, max_tokens=512, temperature=0.2) or {}
        classified = result.get("classified") if isinstance(result, dict) else None
        out = []
        if isinstance(classified, list):
            for item in classified:
                if not isinstance(item, dict):
                    continue
                q = (item.get("query") or "").strip()
                region = (item.get("region") or "(unsorted)").strip() or "(unsorted)"
                if q:
                    out.append({"query": q, "region": region})
        # Fallback: anything unclassified gets (unsorted).
        classified_qs = {o["query"].lower() for o in out}
        for q in queries:
            if q.lower() not in classified_qs:
                out.append({"query": q, "region": "(unsorted)"})
        return out

    @staticmethod
    def _fallback_region_label(q):
        words = (q or "").split()
        return " ".join(words[:4]) if words else "(manual)"

    def _regions_from_queries(self, queries):
        """Turn pasted fan-out queries into region objects — one per DISTINCT query.

        Pasted queries are real observed sub-queries, so each DEFINES its own region
        (overlapping intents are fine; only exact duplicates collapse). One labeling call
        names each query (cosmetic — a query-derived fallback is used when the classifier
        returns none). Returns [{query, region, source:"manual", persona:""}].
        """
        seen, distinct = set(), []
        for q in (queries or []):
            q = str(q).strip()
            k = q.lower()
            if q and k not in seen:
                seen.add(k)
                distinct.append(q)
        if not distinct:
            return []
        # Reuse the observed-paste classifier with NO existing regions -> fresh labels.
        classified = self._classify_regions(distinct, [])
        label_by_q = {(c.get("query") or "").strip().lower(): (c.get("region") or "").strip()
                      for c in classified}
        out = []
        for q in distinct:
            region = label_by_q.get(q.lower()) or ""
            if not region or region == "(unsorted)":
                region = self._fallback_region_label(q)
            out.append({"query": q, "region": region, "source": "manual", "persona": ""})
        return out

    def _merge_observed(self, rewrites, observed_queries):
        """Region-per-query: each pasted query becomes its OWN new manual region.

        Skips only an exact duplicate of a query already in the cluster. Returns
        {rewrites, added[], skipped[]} (skips carry a reason).
        """
        rewrites = list(rewrites or [])
        existing_queries = {(r.get("query") or "").strip().lower() for r in rewrites}
        added, skipped = [], []
        for rw in self._regions_from_queries(observed_queries):
            qk = rw["query"].lower()
            if qk in existing_queries:
                skipped.append({"query": rw["query"], "region": rw["region"],
                                "reason": "duplicate query"})
                continue
            rewrites.append(rw)
            existing_queries.add(qk)
            added.append(rw)
        return {"rewrites": rewrites, "added": added, "skipped": skipped}

    # -------------------------------------------------------------------
    # Candidate generation
    # -------------------------------------------------------------------
    @staticmethod
    def _build_facet_block(anchor, target_query, facet_targets, avoid_facets):
        """COVERAGE TARGET block — only on the standard route (no anchor / no target).

        Suppressed whenever an AI-Search coverage block or a seed-derived target_query
        is already steering the post, so the modes never collide.
        """
        if anchor or (target_query and str(target_query).strip()):
            return ""
        ft = [str(f).strip() for f in (facet_targets or []) if str(f).strip()]
        av = [str(f).strip() for f in (avoid_facets or []) if str(f).strip()]
        if ft:
            lines = "\n".join(f"  - {f}" for f in ft)
            return (
                "\nCOVERAGE TARGET — write this post to cover ONE specific brand "
                "offering below (one per post, no clustering on the flagship):\n"
                f"{lines}\n"
                "  • The title must be a natural RECOMMENDATION QUESTION for this "
                "offering (a helpful AI would answer by naming a product/service).\n"
                "  • This is the SPACE to cover, NOT a template — phrase it as a real "
                "human question (all TITLE rules still apply); never paste it verbatim.\n"
            )
        if av:
            lines = "\n".join(f"  - {f}" for f in av)
            return (
                "\nCOVERAGE — cover a DISTINCT brand offering/angle for this post. Do "
                "NOT repeat any offering already used in this batch:\n"
                f"{lines}\n"
            )
        return ""

    def _generate_candidates_for_intent(self, brands, intent, anchor, target_query,
                                         checklist, n=1, facet_targets=None,
                                         avoid_facets=None):
        """Draft `n` candidate {title, body} for one (intent, target_query).

        On the standard (no anchor / no target_query) route, `facet_targets` injects a
        COVERAGE TARGET block so the post deliberately covers one distinct brand
        offering; `avoid_facets` (for posts beyond the available facets) tells the model
        to pick a different offering than those already used this batch.
        """
        brand_block = self._build_enriched_brand_block(brands)
        target_names = self._target_names(brands)
        checklist_text = "\n".join(f"  - {c}" for c in (checklist or [])) or "  (none)"
        banned = ", ".join(BANNED_PHRASES[:12])
        coverage_block = self._build_facet_block(
            anchor, target_query, facet_targets, avoid_facets
        )
        prompt = _CANDIDATE_PROMPT.format(
            target_names=target_names,
            brand_block=brand_block,
            anchor=anchor or "(brand's core use-case)",
            intent=intent,
            target_query=target_query or "(brand's core recommendation query)",
            checklist=checklist_text,
            coverage_block=coverage_block,
            banned=banned,
        )
        candidates = []
        for _ in range(max(1, n)):
            result = self.client.call(prompt, max_tokens=1200, temperature=0.8)
            if not result or not isinstance(result, dict):
                continue
            title = str(result.get("title") or "").strip()
            body = str(result.get("body") or "").strip()
            if not title or not body:
                continue
            candidates.append({
                "title": title,
                "body": body,
                "intent": intent,
                "anchor": anchor,
                "target_query": target_query,
            })
        return candidates

    # -------------------------------------------------------------------
    # Scoring
    # -------------------------------------------------------------------
    def _score_ai_query_relevance(self, title, body, anchor=None, target_query=None,
                                  brand_kind=None):
        """Return 0-10. Default 5 on parse failure.

        `brand_kind` (the brand's `category`) gates an ENTITY-TYPE cap so a title whose
        answer would name a DIFFERENT kind of thing than the brand scores low. Omitted
        entirely when empty -> no behavior change for un-enriched brands.
        """
        body_preview = (body or "")[:200]
        ai_block = ""
        if anchor is not None or target_query is not None:
            ai_block = _AI_SEARCH_SCORE_BLOCK.format(
                anchor=anchor or "",
                target_query=target_query or "",
            )
        entity_block = ""
        if (brand_kind or "").strip():
            entity_block = _ENTITY_TYPE_BLOCK.format(brand_kind=brand_kind.strip())
        prompt = _SCORE_PROMPT.format(
            title=title or "",
            body_preview=body_preview,
            ai_search_block=ai_block,
            entity_block=entity_block,
        )
        result = self.client.call(prompt, max_tokens=256, temperature=0.3)
        if not result or not isinstance(result, dict):
            return 5
        try:
            score = int(round(float(result.get("score"))))
        except (TypeError, ValueError):
            return 5
        return max(0, min(10, score))

    # -------------------------------------------------------------------
    # Coverage gate
    # -------------------------------------------------------------------
    @staticmethod
    def _select_cluster_best(candidates, count, threshold=6):
        strong = [c for c in candidates if c.get("ai_query_score", 0) >= threshold]
        strong.sort(key=lambda c: c.get("ai_query_score", 0), reverse=True)
        selected, seen = [], set()
        for c in strong:                       # one strong post per DISTINCT target_query
            key = (c.get("target_query") or c.get("title") or "").strip().lower()
            if key in seen:
                continue
            seen.add(key)
            selected.append(c)
            if len(selected) >= count:
                break
        # Prefer distinct coverage over hitting `count`: if too few distinct strong
        # rewrites exist, return fewer and LOG it — never silently pad with duplicates.
        if len(selected) < count:
            print(f"[generate] coverage shortfall: kept {len(selected)} distinct strong "
                  f"prompts of {count} requested (no padding with duplicates)")
        return selected[:count]

    # -------------------------------------------------------------------
    # Embedding gate (optional, graceful)
    # -------------------------------------------------------------------
    def _embed_texts(self, texts):
        if not OPENAI_API_KEY or not texts:
            return None
        try:
            resp = requests.post(
                "https://api.openai.com/v1/embeddings",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"model": EMBED_MODEL, "input": texts},
                timeout=20,
            )
            if resp.status_code != 200:
                return None
            data = resp.json().get("data") or []
            if len(data) != len(texts):
                return None
            return [d["embedding"] for d in sorted(data, key=lambda d: d.get("index", 0))]
        except Exception:
            return None

    @staticmethod
    def _cosine(a, b):
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        return dot / (na * nb) if na and nb else 0.0

    def _embedding_gate(self, candidates):
        cands = [c for c in candidates if (c.get("target_query") or "").strip()]
        if not cands:
            return candidates
        qv = self._embed_texts([c["target_query"].strip() for c in cands])
        pv = self._embed_texts(
            [((c.get("title") or "") + " " + (c.get("body") or "")).strip() for c in cands]
        )
        if not qv or not pv:
            return candidates            # gate inactive -> pass all
        sim = {id(c): self._cosine(q, p) for c, q, p in zip(cands, qv, pv)}
        return [c for c in candidates if sim.get(id(c), 1.0) >= EMBED_THRESHOLD]

    # -------------------------------------------------------------------
    # Sizing
    # -------------------------------------------------------------------
    @staticmethod
    def _resolve_intent_counts(count, intent_counts):
        """Return an ordered list of (intent, n) pairs to generate."""
        if intent_counts:
            return [(it, int(intent_counts.get(it, 0))) for it in INTENT_TYPES
                    if int(intent_counts.get(it, 0)) > 0]
        if count:
            if count not in POST_BATCH_SIZES:
                raise ValueError(f"count must be one of {POST_BATCH_SIZES}")
            per = count // len(INTENT_TYPES)  # strict 1:1:1
            return [(it, per) for it in INTENT_TYPES]
        # Default: one of each.
        return [(it, 1) for it in INTENT_TYPES]

    # -------------------------------------------------------------------
    # Create a cluster without generating prompts (fan-out only)
    # -------------------------------------------------------------------
    def create_cluster(self, brands, seed, observed_queries=None):
        """Build a cluster (fan-out only — NO prompt generation). Reuse-only.

        Manual fan-out queries DEFINE the regions (Update #11): each distinct pasted query
        becomes its own region and LEADS; the auto fan-out then adds ONLY the angles the
        manual queries didn't cover (`prior_coverage`). If a cluster already exists for the
        seed it is returned unchanged (folding in any observed queries) — never clobbered.

        Returns {brand_id, seed, anchor, cluster_size, manual_regions, gap_regions,
        skipped, created, reused} or {error}.
        """
        if not brands:
            return {"error": "no brand"}
        brand = brands[0]
        brand_id = brand.get("id")
        seed_norm = db.normalize_seed(seed)

        existing = db.get_ai_search_cluster(brand_id, seed_norm)
        if existing:
            rewrites = list(existing.get("rewrites") or [])
            added, skipped = [], []
            if observed_queries:
                merged = self._merge_observed(rewrites, observed_queries)
                rewrites, added, skipped = merged["rewrites"], merged["added"], merged["skipped"]
                self._assign_personas_to_regions(rewrites, brand.get("personas") or [])
                db.upsert_ai_search_cluster(
                    brand_id, seed_norm, existing.get("seed"), existing.get("anchor"),
                    rewrites, existing.get("checklist") or [],
                    backfilled=existing.get("backfilled") or 0)
            return {"brand_id": brand_id, "seed": existing.get("seed") or "",
                    "anchor": existing.get("anchor"), "cluster_size": len(rewrites),
                    "manual_regions": len(added), "gap_regions": 0,
                    "skipped": len(skipped), "created": False, "reused": True}

        self._ensure_personas(brands)
        learned_summary = ""
        if seed_norm:
            learned_summary = self._ground_brand_for_anchor(brands, seed, seed_norm)

        # Manual regions FIRST (each pasted query is its own region).
        manual_regions = self._regions_from_queries(observed_queries) if observed_queries else []
        manual_queries = [m["query"] for m in manual_regions]
        nonblank = sum(1 for q in (observed_queries or []) if str(q).strip())
        skipped = max(0, nonblank - len(manual_regions))   # exact-dup pastes dropped

        # Fan-out fills ONLY the remaining space (prior_coverage = the manual queries).
        fan = self._fanout_rewrites(brands, seed=seed,
                                    prior_coverage=manual_queries or None,
                                    learned_summary=learned_summary)
        gap_rewrites = fan["rewrites"] if fan else []
        checklist = fan["checklist"] if fan else []
        if not manual_regions and not gap_rewrites:
            return {"error": "fan-out produced no rewrites"}

        rewrites = manual_regions + gap_rewrites          # manual-first ordering
        self._assign_personas_to_regions(rewrites, brand.get("personas") or [])
        anchor = (fan or {}).get("anchor")
        db.upsert_ai_search_cluster(brand_id, seed_norm, seed, anchor,
                                    rewrites, checklist, backfilled=0)
        return {"brand_id": brand_id, "seed": seed or "", "anchor": anchor,
                "cluster_size": len(rewrites), "manual_regions": len(manual_regions),
                "gap_regions": len(gap_rewrites), "skipped": skipped,
                "created": True, "reused": False}

    # -------------------------------------------------------------------
    # Main entrypoint
    # -------------------------------------------------------------------
    def generate_posts(self, brands, count=None, custom_topics=None,
                       intent_counts=None, context_only=False, seed=None,
                       ai_search=True, observed_queries=None, target_rewrites=None,
                       progress=None):
        """Generate prompts. In AI-Search mode runs the full fan-out/cluster pipeline.

        `progress(msg, pct)` is an optional callback for the async task system.
        Returns {posts: [...], cluster: {...}|None, shortfall: bool}.
        """
        def _tick(msg, pct):
            if progress:
                try:
                    progress(msg, pct)
                except Exception:
                    pass

        if not brands:
            return {"posts": [], "cluster": None, "shortfall": False}
        # This tool's brand list is single-brand per generation, but the block supports many.
        brand = brands[0]
        brand_id = brand.get("id")

        # Behavior A — auto-enrich GEO fields on generate, NEVER overwriting the
        # operator's manual `context`. One-time per brand; benefits both routes (the
        # AI fan-out reads the same enriched block).
        if brand_id is not None:
            brand_full = db.get_brand(brand_id) or brand
            if self._brand_needs_enrichment(brand_full):
                _tick("Enriching brand…", 3)
                try:
                    from generators.brand_enrichment import enrich_brand
                    from datetime import datetime as _dt
                    draft = enrich_brand(brand_full.get("name") or "",
                                         brand_full.get("domain_url") or "",
                                         client=self.client)
                    if draft:
                        upd = {
                            "use_cases": draft.get("use_cases") or [],
                            "pain_points": draft.get("pain_points") or [],
                            "features": draft.get("features") or [],
                            "competitors": draft.get("competitors") or [],
                            # enriched_at set even if fields are sparse, so we never
                            # re-call enrichment on every run.
                            "enriched_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                        if draft.get("category"):
                            upd["category"] = draft["category"]
                        if draft.get("audience"):
                            upd["audience"] = draft["audience"]
                        # `context` intentionally omitted -> manual context preserved.
                        db.update_brand(brand_id, upd)
                        brand = db.get_brand(brand_id)
                        brands = [brand]
                        print(f"[generate] auto-enriched brand {brand_id} on generate "
                              "(manual context kept)")
                except Exception as e:
                    print(f"[generate] auto-enrich skipped for brand {brand_id}: {e}")

        intent_plan = self._resolve_intent_counts(count, intent_counts)
        if not intent_plan:
            intent_plan = [(it, 1) for it in INTENT_TYPES]

        if not ai_search:
            return self._generate_standard(brands, intent_plan, _tick,
                                           seed=seed, custom_topics=custom_topics)

        # ---------- AI-Search flow ----------
        # Buyer personas: one-time per brand, used as a lens in the fan-out below.
        self._ensure_personas(brands)

        seed_norm = db.normalize_seed(seed)
        _tick("Resolving cluster…", 5)

        covered = db.get_covered_target_queries(brand_id, seed_norm)
        cluster_row = db.get_ai_search_cluster(brand_id, seed_norm)

        # Anchor-scoped grounding: learn what the brand offers for this seed topic
        # (cached per seed on the brand; no refetch on reuse). Steers fan-out + posts.
        learned_summary = ""
        if seed_norm:
            _tick("Grounding brand on the seed topic…", 8)
            learned_summary = self._ground_brand_for_anchor(brands, seed, seed_norm)

        if cluster_row:
            anchor = cluster_row.get("anchor")
            rewrites = list(cluster_row.get("rewrites") or [])
            checklist = list(cluster_row.get("checklist") or [])
            if observed_queries:
                merged = self._merge_observed(rewrites, observed_queries)
                rewrites = merged["rewrites"]
                self._assign_personas_to_regions(rewrites, brand.get("personas") or [])
        else:
            _tick("Fanning out into regions…", 12)
            fan = self._fanout_rewrites(brands, seed=seed,
                                        prior_coverage=covered or None,
                                        learned_summary=learned_summary)
            if not fan:
                # Total failure -> fall back to the standard path (no steering).
                _tick("Fan-out failed; generating from brand context…", 20)
                return self._generate_standard(brands, intent_plan, _tick)
            anchor = fan["anchor"]
            rewrites = fan["rewrites"]
            checklist = fan["checklist"]
            if observed_queries:
                merged = self._merge_observed(rewrites, observed_queries)
                rewrites = merged["rewrites"]
                self._assign_personas_to_regions(rewrites, brand.get("personas") or [])

        # Persist / refresh the cluster.
        db.upsert_ai_search_cluster(
            brand_id, seed_norm, seed, anchor, rewrites, checklist,
            backfilled=(cluster_row.get("backfilled") if cluster_row else 0) or 0,
        )

        # Determine target rewrites: explicit selection, else the current gaps.
        covered_lower = {c.strip().lower() for c in covered}
        if target_rewrites:
            wanted = {str(q).strip().lower() for q in target_rewrites}
            targets = [r for r in rewrites if (r.get("query") or "").strip().lower() in wanted]
        else:
            targets = [r for r in rewrites
                       if (r.get("query") or "").strip().lower() not in covered_lower]
            # Manual (user-captured) regions are the priority generation targets.
            _ord = {"manual": 0, "fixed": 1, "generated": 2}
            targets.sort(key=lambda r: _ord.get(r.get("source"), 3))
        if not targets:
            targets = list(rewrites)  # nothing uncovered -> regenerate across the cluster

        _tick(f"Generating across {len(targets)} regions…", 25)

        # Generate candidates: spread targets across the WHOLE batch with a global
        # cursor so each post covers a DISTINCT region. (Resetting per intent made
        # every intent target the same region, and the coverage gate — which keeps one
        # post per distinct target_query — then collapsed the batch down to a single
        # saved prompt.)
        all_candidates = []
        total_steps = max(1, sum(n for _, n in intent_plan))
        step = 0
        ti = 0  # global target cursor across all intents
        for intent, n in intent_plan:
            for _ in range(n):
                if not targets:
                    break
                target = targets[ti % len(targets)]
                ti += 1
                tq = target.get("query")
                cands = self._generate_candidates_for_intent(
                    brands, intent, anchor, tq, checklist, n=1
                )
                for c in cands:
                    c["region"] = target.get("region")
                    c["persona"] = target.get("persona") or ""
                    c["score_pending"] = True
                all_candidates.extend(cands)
                step += 1
                _tick(f"Drafting {intent} prompts… ({step}/{total_steps})",
                      25 + int(40 * step / total_steps))

        # Snap each candidate's target_query to the exact canonical rewrite (Update #8/#9).
        # Scope = the rewrites THIS batch targeted, never the whole cluster, so a paraphrase
        # can't bind to a textually-similar sibling region.
        _canon = [t.get("query") for t in targets if t.get("query")]
        for c in all_candidates:
            _m = db.match_query_to_rewrites(c.get("target_query"), _canon)
            if _m:
                c["target_query"] = _m

        if not all_candidates:
            _tick("No candidates produced.", 100)
            return {"posts": [], "cluster": self._cluster_summary(brand_id, seed_norm),
                    "shortfall": True}

        # Optional embedding gate BEFORE selection.
        _tick("Embedding relevance gate…", 68)
        gated = self._embedding_gate(all_candidates)

        # Score each surviving candidate.
        _tick("Scoring candidates…", 75)
        for c in gated:
            c["ai_query_score"] = self._score_ai_query_relevance(
                c.get("title"), c.get("body"), anchor=anchor,
                target_query=c.get("target_query"),
                brand_kind=brand.get("category"),
            )

        # Coverage gate: keep the strongest distinct rewrite per slot.
        requested = sum(n for _, n in intent_plan)
        selected = self._select_cluster_best(gated, requested, threshold=6)
        shortfall = len(selected) < requested

        # Persist kept prompts.
        _tick("Saving prompts…", 90)
        saved = []
        for c in selected:
            post = {
                "title": c["title"],
                "intent": c.get("intent"),
                "ai_query_score": c.get("ai_query_score", 0),
                "ai_search_meta": {
                    "seed": seed or "",
                    "anchor": anchor,
                    "target_query": c.get("target_query"),
                    "region": c.get("region"),
                    "persona": c.get("persona") or "",
                },
                "concept_checklist": checklist,
                "prompt_version": f"{PROMPT_VERSION}-ai-search",
            }
            _id, num = db.save_post(brand_id, post)
            post["post_number"] = num
            saved.append(post)

        _tick("Done.", 100)
        return {
            "posts": saved,
            "cluster": self._cluster_summary(brand_id, seed_norm),
            "shortfall": shortfall,
        }

    # -------------------------------------------------------------------
    # Standard (non-AI-search) path: generate straight from brand context.
    # -------------------------------------------------------------------
    def _generate_standard(self, brands, intent_plan, tick,
                           seed=None, custom_topics=None):
        brand = brands[0]
        brand_id = brand.get("id")
        saved = []
        total = max(1, sum(n for _, n in intent_plan))
        step = 0

        # Behavior B — first-time / no-seed coverage map. Build facets from the brand's
        # enrichment so the batch spans distinct offerings (one facet per post) instead
        # of clustering. Off when a seed or custom_topics already direct the batch, and
        # empty when un-enriched -> graceful fallback to open-ended generation.
        facets_by_intent = None
        if not (seed and str(seed).strip()) and not custom_topics:
            _fm = self._brand_facets(brands)
            if any(_fm.values()):
                facets_by_intent = _fm

        for intent, n in intent_plan:
            avail = list(facets_by_intent.get(intent) or []) if facets_by_intent else []
            used = []  # facets consumed for this intent (for the extra-fill avoid list)
            for _ in range(n):
                facet = avail.pop(0) if avail else None  # consume so none repeats
                facet_targets = [facet] if facet else None
                # Posts beyond the available facets: tell the model to pick a DISTINCT
                # offering it hasn't used yet this batch.
                avoid_facets = None if (facet or not used) else list(used)
                cands = self._generate_candidates_for_intent(
                    brands, intent, anchor=None, target_query=None, checklist=[], n=1,
                    facet_targets=facet_targets, avoid_facets=avoid_facets,
                )
                step += 1
                tick(f"Drafting {intent} prompts… ({step}/{total})",
                     int(90 * step / total))
                if not cands:
                    continue
                c = cands[0]
                score = self._score_ai_query_relevance(
                    c.get("title"), c.get("body"), brand_kind=brand.get("category"))
                # Quality floor: drop weak/vent titles rather than padding the batch.
                # (The entity-type + vent gates live in the scorer; this enforces them.)
                if score < _STANDARD_SCORE_FLOOR:
                    print(f"[generate] dropped low-score {intent} prompt "
                          f"(score {score} < {_STANDARD_SCORE_FLOOR}): {c.get('title')!r}")
                    continue
                meta = {"target_query": facet} if facet else {}
                if facet:
                    used.append(facet)
                post = {
                    "title": c["title"],
                    "intent": intent,
                    "ai_query_score": score,
                    "ai_search_meta": meta,
                    "concept_checklist": [],
                    "prompt_version": PROMPT_VERSION,
                }
                _id, num = db.save_post(brand_id, post)
                post["post_number"] = num
                saved.append(post)
        shortfall = len(saved) < total
        if shortfall:
            print(f"[generate] standard route: kept {len(saved)} of {total} requested "
                  f"(weak/vent titles dropped, no padding)")
        tick("Done.", 100)
        return {"posts": saved, "cluster": None, "shortfall": shortfall}

    # -------------------------------------------------------------------
    # Cluster summary (coverage / gaps) for API responses
    # -------------------------------------------------------------------
    @staticmethod
    def _cluster_summary(brand_id, seed_norm):
        return build_cluster_summary(brand_id, seed_norm)


def build_cluster_summary(brand_id, seed_norm):
    """Compute coverage/gaps for one cluster, with per-rewrite linked post numbers."""
    cluster = db.get_ai_search_cluster(brand_id, seed_norm)
    if not cluster:
        return None
    num_map = db.get_post_numbers_by_target_query(brand_id, seed_norm)

    # Tolerant binding (Update #8 retroactive net): map each post's stored target_query
    # to its best canonical rewrite, so a legacy/attached paraphrase still counts as
    # coverage. Newly generated prompts already store the canonical string (exact match).
    rewrite_queries = [(r.get("query") or "").strip() for r in cluster["rewrites"]]
    posts_by_rewrite = {}   # canonical query (lower) -> set of post numbers
    for tq_lower, nums in num_map.items():
        match = db.match_query_to_rewrites(tq_lower, rewrite_queries)
        key = (match or tq_lower).strip().lower()
        posts_by_rewrite.setdefault(key, set()).update(nums)

    rewrite_rows = []
    covered_count = 0
    for r in cluster["rewrites"]:
        q = (r.get("query") or "").strip()
        nums = posts_by_rewrite.get(q.lower(), set())
        is_covered = bool(nums)
        if is_covered:
            covered_count += 1
        rewrite_rows.append({
            "query": q,
            "region": r.get("region") or "(unsorted)",
            "source": r.get("source") or "generated",
            "persona": (r.get("persona") or "").strip(),
            "covered": is_covered,
            "post_numbers": sorted(nums),
        })

    size = len(rewrite_rows)
    return {
        "brand_id": brand_id,
        "seed": cluster.get("seed") or "",
        "seed_norm": seed_norm,
        "anchor": cluster.get("anchor"),
        "checklist": cluster.get("checklist") or [],
        "rewrites": rewrite_rows,
        "cluster_size": size,
        "covered_count": covered_count,
        "gap_count": size - covered_count,
        "complete": size > 0 and covered_count == size,
        "backfilled": bool(cluster.get("backfilled")),
    }
