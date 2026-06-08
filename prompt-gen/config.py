"""Configuration: env loading, model, constants."""
import os

try:
    # Optional: load a local .env if python-dotenv is installed. Not required.
    # Some launchers (e.g. the Claude Desktop app) export an EMPTY
    # ANTHROPIC_API_KEY into the environment, which would otherwise shadow the
    # real key in .env. Let .env win when the existing value is blank/unset.
    from dotenv import load_dotenv
    _key_blank = not os.environ.get("ANTHROPIC_API_KEY")
    load_dotenv(override=_key_blank)
except Exception:
    pass

# --- Anthropic ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
DEFAULT_MODEL = "claude-sonnet-4-20250514"
PROMPT_VERSION = "v1.0"

# --- Generation sizing ---
POST_BATCH_SIZES = (3, 6, 9)
INTENT_TYPES = ("commercial", "comparison", "informational")
USER_AGENT = "BotricPromptGen/1.0"

# --- Fan-out region axes ---
FIXED_REGIONS = [
    "Category / best tool",
    "Comparison / alternative",
    "Constraint",            # the dominant buying constraint (copyright, budget, compliance…)
    "Use-case / workflow",
    "Persona / segment",
    "Adjacent platform",
]
MAX_FANOUT_REWRITES = 10     # hard ceiling: one per region, keeps the cluster sparse

# --- Optional embedding relevance gate (off unless a key is set) ---
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
EMBED_THRESHOLD = float(os.environ.get("EMBED_THRESHOLD", "0.68"))

# --- Storage ---
DB_PATH = os.environ.get("DB_PATH", "strategy_bot.db")
