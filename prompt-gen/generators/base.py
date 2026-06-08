"""ClaudeClient wrapper + shared helpers (banned phrases, tolerant JSON parsing)."""
import json
import re

from config import ANTHROPIC_API_KEY, DEFAULT_MODEL

# Phrases that make AI-generated prompts read as spam / marketing. The
# generators are instructed to avoid these; kept here as a single source of truth.
BANNED_PHRASES = [
    "game changer", "game-changer", "look no further", "in today's world",
    "in this day and age", "revolutionize", "revolutionary", "cutting-edge",
    "unleash", "unlock the power", "take it to the next level", "seamless",
    "seamlessly", "elevate your", "supercharge", "best-in-class",
    "world-class", "leverage", "synergy", "robust solution", "dive in",
    "delve into", "it's worth noting", "at the end of the day",
]


def strip_code_fences(text):
    """Remove ```json ... ``` fences and surrounding prose so json.loads has a chance."""
    if not text:
        return ""
    t = text.strip()
    # Strip a leading ```json / ``` and a trailing ```
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def extract_json(text):
    """Best-effort: pull the first balanced JSON object/array out of a string."""
    if not text:
        return None
    t = strip_code_fences(text)
    try:
        return json.loads(t)
    except (json.JSONDecodeError, TypeError):
        pass
    # Fall back to the outermost { } or [ ] span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        end = t.rfind(closer)
        if start != -1 and end != -1 and end > start:
            chunk = t[start:end + 1]
            try:
                return json.loads(chunk)
            except (json.JSONDecodeError, TypeError):
                continue
    return None


class ClaudeClient:
    """Thin wrapper around the Anthropic SDK.

    `.call(...)` returns parsed JSON (tolerant of markdown fences). `.call_text(...)`
    returns the raw text. Both degrade gracefully when no key/SDK is available.
    """

    def __init__(self, api_key=None, model=None):
        self.api_key = api_key or ANTHROPIC_API_KEY
        self.model = model or DEFAULT_MODEL
        self._client = None
        if self.api_key:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except Exception as e:  # SDK missing or init failure — stay graceful.
                print(f"[claude] could not init Anthropic client: {e}")
                self._client = None

    @property
    def available(self):
        return self._client is not None

    def call_text(self, prompt, max_tokens=1024, temperature=0.7):
        """Return the raw text reply, or "" on any failure."""
        if not self._client:
            print("[claude] no client available (missing ANTHROPIC_API_KEY?)")
            return ""
        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
            )
            parts = []
            for block in resp.content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            return "".join(parts).strip()
        except Exception as e:
            print(f"[claude] call error: {e}")
            return ""

    def call(self, prompt, max_tokens=1024, temperature=0.7):
        """Return parsed JSON, or None on failure."""
        raw = self.call_text(prompt, max_tokens=max_tokens, temperature=temperature)
        if not raw:
            return None
        return extract_json(raw)
