"""Website enrichment: fetch homepage -> extract visible text -> Claude GEO fields (+ ICPs).

The enrich entrypoint returns a DRAFT and never saves. Every step is graceful: a failed
homepage fetch must not error — Claude falls back to the brand name + general knowledge.
"""
from html.parser import HTMLParser

import requests

from config import USER_AGENT
from generators.base import ClaudeClient

# Tags whose text content is never "visible".
_SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe"}
_MAX_PAGE_CHARS = 6000


def _fetch_homepage(domain_url, timeout=10):
    if not domain_url:
        return ""
    url = domain_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=timeout,
            allow_redirects=True,
        )
        if resp.status_code == 200 and resp.text:
            return resp.text
    except requests.exceptions.RequestException as e:
        print(f"[enrich] fetch error for {url}: {e}")
    return ""


class _VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip_depth = 0
        self._chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def get_text(self):
        return " ".join(self._chunks)


def _extract_visible_text(html):
    if not html:
        return ""
    parser = _VisibleTextParser()
    try:
        parser.feed(html)
    except Exception as e:
        print(f"[enrich] parse error: {e}")
    text = " ".join(parser.get_text().split())  # collapse whitespace
    return text[:_MAX_PAGE_CHARS]


_ENRICH_PROMPT = """You are analyzing a brand to extract structured context for a GEO (Generative
Engine Optimization) content strategy. The goal is to surface the long-tail questions
real users type into AI search engines (ChatGPT / Perplexity / Gemini) about this
brand's domain — WITHOUT naming the brand itself.

BRAND NAME: {name}
BRAND URL: {url}

{page_section}

Extract the following fields. Be specific and concrete — vague answers are useless.

- name: The brand's actual product/company name as it presents itself (e.g. "Notion", "Epidemic Sound"). If a BRAND NAME was given above, refine/confirm it; if only a URL was given, infer the real brand name from the homepage. A clean proper name, not a tagline.
- keywords: 5-10 short SEO/search keywords and key terms a user would search for this brand's domain (single words or 2-3 word phrases, e.g. "royalty free music", "background music for videos"). Lowercase, no brand name.
- category: A precise product category, 3-8 words. Example: "project management SaaS for remote teams", "direct-to-consumer electric toothbrush". Not "software" or "product".
- audience: The ideal customer profile (ICP). Who buys/uses this? Role, team size, industry, context. 1-2 sentences.
- use_cases: 4-6 concrete jobs-to-be-done — what problems do users hire this product to solve? Each item should be a short phrase ("running async standups across timezones"), not a sentence.
- pain_points: 4-6 concrete pains the product addresses. Each item is a phrase ("tickets get lost between Slack and Jira"). These are the pains real users would describe when searching for a solution.
- features: 4-6 key differentiating features or capabilities. Each is a short phrase.
- competitors: 3-8 direct competitor brand/product NAMES (real names like "Notion", "Asana", "Linear"). These will be used in comparison-intent posts, so accuracy matters. If you're unsure, include fewer but only confident ones.
- icps: 3-5 DISTINCT ideal customer profiles — the different kinds of buyers/users for this brand. Each is an object {{label, role, segment, context, pains}}: label = a 2-4 word name ("solo podcaster"); role = who they are; segment = market/size/industry; context = the situation in which they need this; pains = 1-3 short pain phrases specific to THIS profile. Make the profiles genuinely different from each other, not rewordings.
- context_summary: A 2-3 sentence narrative describing what the brand is and who it serves. This replaces or augments the existing brand context field.

Return JSON only, exactly this shape:
{{
  "name": "string",
  "keywords": ["string", ...],
  "category": "string",
  "audience": "string",
  "use_cases": ["string", ...],
  "pain_points": ["string", ...],
  "features": ["string", ...],
  "competitors": ["string", ...],
  "icps": [{{"label":"string","role":"string","segment":"string","context":"string","pains":["string", ...]}}, ...],
  "context_summary": "string"
}}"""


def _build_page_section(page_text):
    if page_text:
        return f'HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""'
    return ("HOMEPAGE TEXT: (could not fetch homepage — rely on the brand name and your "
            "general knowledge; flag uncertainty by leaving fields as empty strings or "
            "empty arrays.)")


def _trimmed_str_list(val):
    if not isinstance(val, list):
        return []
    out = []
    for x in val:
        if x is None:
            continue
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def _normalize_icps(val):
    if not isinstance(val, list):
        return []
    out = []
    for item in val:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        role = str(item.get("role") or "").strip()
        segment = str(item.get("segment") or "").strip()
        context = str(item.get("context") or "").strip()
        pains = _trimmed_str_list(item.get("pains"))
        if not any((label, role, segment, context, pains)):
            continue
        out.append({
            "label": label, "role": role, "segment": segment,
            "context": context, "pains": pains,
        })
    return out


_ANCHOR_GROUND_PROMPT = """You are grounding a GEO campaign. We are about to build content anchored on a specific
TOPIC for this brand, and need to know what THIS brand actually offers or does that is
relevant to that topic — so the content stays truthful and on-target.

BRAND NAME: {name}
BRAND URL: {domain_url}
ANCHOR TOPIC: "{anchor}"

{page_section}

Describe ONLY what is supported by the page above or your CONFIDENT knowledge of this
brand — do NOT invent products, services, or claims. If the brand has no real offering
relevant to "{anchor}", say so (covers=false): it's better to flag a poor fit than to
fabricate.

Return JSON only:
{{"summary": "1-3 sentences: what this brand offers for \\"{anchor}\\" (or, if covers=false, a one-line note it doesn't serve this topic)", "covers": true, "key_points": ["...", "..."]}}"""


def _ground_page_section(page_text):
    if page_text:
        return f'HOMEPAGE TEXT (visible content only):\n"""\n{page_text}\n"""'
    return ("HOMEPAGE TEXT: (could not fetch — use your CONFIDENT knowledge of this brand "
            "only; if you have none, set covers=false rather than inventing anything.)")


def enrich_brand_for_anchor(name, domain_url, anchor, client=None):
    """Learn what a brand actually offers for a specific anchor TOPIC.

    Grounds on the brand's own site + confident knowledge — never the open web — so it
    never invents offerings. Returns {summary, covers, key_points}, or {} on failure.
    """
    client = client or ClaudeClient()
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html)

    prompt = _ANCHOR_GROUND_PROMPT.format(
        name=name or "(unknown)",
        domain_url=domain_url or "(none)",
        anchor=(anchor or "").strip() or "(general)",
        page_section=_ground_page_section(page_text),
    )
    result = client.call(prompt, max_tokens=600, temperature=0.2)
    if not result or not isinstance(result, dict):
        return {}
    summary = str(result.get("summary") or "").strip()
    if not summary:
        return {}
    return {
        "summary": summary,
        "covers": bool(result.get("covers", True)),
        "key_points": _trimmed_str_list(result.get("key_points")),
    }


_PERSONAS_PROMPT = """You are defining the buyer PERSONAS for a GEO campaign — the distinct kinds of people who
would search for what this brand offers. These decide which recommendation questions are
worth targeting and whether THIS brand is a credible answer for each.

BRAND NAME: {name}
BRAND URL: {url}
CATEGORY: {category}
AUDIENCE: {audience}
USE-CASES: {use_cases}
PAIN-POINTS: {pain_points}

{page_section}

Produce 3-5 DISTINCT, non-overlapping personas (different situations/intents — not
rewordings). Ground them in the brand's real space; do NOT invent. For each, judge FIT
honestly:
  "yes"  = squarely the brand's customer
  "maybe"= plausible / adjacent
  "no"   = wants something the brand isn't (include a couple when realistic — this is the
           winnability filter).

For each persona return:
  - label:       2-4 word name ("solo podcaster")
  - profile:     1 sentence on who they are / their situation
  - trigger:     what makes them start searching
  - goal:        what they want to achieve / buy
  - constraints: their main constraint (budget, compliance, time, skill…)
  - vocab:       2-4 words/phrases this persona actually uses
  - fit:         "yes" | "maybe" | "no"

Return JSON only:
{{"personas": [{{"label": "string", "profile": "string", "trigger": "string", "goal": "string", "constraints": "string", "vocab": "string", "fit": "yes"}}]}}"""


def _normalize_personas(val):
    """Require a non-empty label; coerce fit to yes/maybe/no (default maybe); cap at 5."""
    if not isinstance(val, list):
        return []
    out = []
    for item in val:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        fit = str(item.get("fit") or "").strip().lower()
        if fit not in ("yes", "maybe", "no"):
            fit = "maybe"
        out.append({
            "label": label,
            "profile": str(item.get("profile") or "").strip(),
            "trigger": str(item.get("trigger") or "").strip(),
            "goal": str(item.get("goal") or "").strip(),
            "constraints": str(item.get("constraints") or "").strip(),
            "vocab": str(item.get("vocab") or "").strip(),
            "fit": fit,
        })
        if len(out) >= 5:
            break
    return out


def generate_brand_personas(name, domain_url, category=None, audience=None,
                            use_cases=None, pain_points=None, client=None):
    """Return 3-5 distinct, fit-honest buyer personas for a brand (or [] on failure).

    Grounds on the brand's own homepage + confident knowledge — never invents.
    """
    client = client or ClaudeClient()
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html)

    def _join(v):
        if isinstance(v, list):
            return ", ".join(str(x).strip() for x in v if str(x).strip()) or "(none)"
        return str(v or "").strip() or "(none)"

    prompt = _PERSONAS_PROMPT.format(
        name=name or "(unknown)",
        url=domain_url or "(none)",
        category=_join(category),
        audience=_join(audience),
        use_cases=_join(use_cases),
        pain_points=_join(pain_points),
        page_section=_build_page_section(page_text),
    )
    result = client.call(prompt, max_tokens=1500, temperature=0.4)
    if not result or not isinstance(result, dict):
        return []
    return _normalize_personas(result.get("personas"))


def enrich_brand(name, domain_url, client=None):
    """Return an enrichment draft dict (never saves). Includes `_page_fetched`."""
    client = client or ClaudeClient()
    html = _fetch_homepage(domain_url)
    page_text = _extract_visible_text(html)

    prompt = _ENRICH_PROMPT.format(
        name=name or "(unknown)",
        url=domain_url or "(none)",
        page_section=_build_page_section(page_text),
    )

    result = client.call(prompt, max_tokens=1500, temperature=0.3) or {}

    draft = {
        "_ai_available": bool(getattr(client, "available", False)),
        "name": str(result.get("name") or name or "").strip(),
        "keywords": _trimmed_str_list(result.get("keywords")),
        "category": str(result.get("category") or "").strip(),
        "audience": str(result.get("audience") or "").strip(),
        "use_cases": _trimmed_str_list(result.get("use_cases")),
        "pain_points": _trimmed_str_list(result.get("pain_points")),
        "features": _trimmed_str_list(result.get("features")),
        "competitors": _trimmed_str_list(result.get("competitors")),
        "icps": _normalize_icps(result.get("icps")),
        "context": str(result.get("context_summary") or "").strip(),
        "_page_fetched": bool(page_text),
    }
    return draft
