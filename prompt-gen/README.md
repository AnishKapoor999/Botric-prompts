# Botric — Prompt-Generation System

A local, single-operator tool that generates GEO *prompts* (search-style queries real
users type into ChatGPT / Perplexity / Gemini) for a brand using the AI-Search
**region / sparse-fan-out → cluster → generate** pipeline. No auth, runs locally. Built
to the handover spec (`Botric_SEO_Prompt_Generation_Spec.md`).

## What it does

1. **Add a brand by URL** → auto-fetch & extract brand context + detect multiple ICPs →
   review/edit → save.
2. **Generate prompts** — fan a seed (optional) into distinct regions, persist a cluster
   per `(brand, seed)`, draft one strong prompt per region × intent, score, coverage-gate,
   and (optionally) embedding-gate.
3. **Review clusters** (coverage / gaps), fill gaps, generate for selected regions, paste
   observed queries, attach prompts by number, delete.
4. **View & export** generated prompts as CSV.

## Setup & run

```bash
cd prompt-gen
pip install -r requirements.txt        # flask, anthropic, requests
cp .env.example .env                   # then add your key
export ANTHROPIC_API_KEY=sk-ant-...    # (or put it in .env)
python app.py                          # http://127.0.0.1:5000
```

SQLite (`strategy_bot.db`) is created on first run with WAL enabled. The optional embedding
relevance gate stays **off** unless `OPENAI_API_KEY` is set.

## Deploy to Railway

The app is Railway-ready (`Procfile`, `railway.json`, `.python-version`, gunicorn).

1. **Push the repo to GitHub**, then in Railway: **New Project → Deploy from GitHub repo**.
2. **Set the service Root Directory to `prompt-gen`** (Settings → Source). The app lives in
   this subfolder, so Railway must build from here.
3. **Add variables** (Settings → Variables):
   - `ANTHROPIC_API_KEY` — required.
   - `DB_PATH=/data/strategy_bot.db` — so data lives on the volume (see next step).
   - optional: `OPENAI_API_KEY`, `EMBED_MODEL`, `EMBED_THRESHOLD`.
4. **Add a Volume** (Settings → Volumes) mounted at **`/data`**. SQLite then persists across
   redeploys. *Without a volume the database resets on every deploy.*
5. Railway injects `$PORT` and runs gunicorn from the `Procfile`. The app binds `0.0.0.0:$PORT`
   automatically — no further config needed. Open the generated URL when the deploy is green.

> **Single worker by design.** The start command runs gunicorn with `--workers 1 --threads 8`.
> Background generation jobs are tracked in an in-process registry, so the app must run as one
> process — do **not** raise the worker count (add threads instead). There is **no auth**, so
> treat the public URL as sensitive or put it behind Railway's access controls.

CLI alternative: `npm i -g @railway/cli && railway login && cd prompt-gen && railway up`.

## Layout

```
app.py                      Flask routes + in-process async task registry
db.py                       SQLite schema + all data access (clusters, posts, brands)
config.py                   env loading, model (claude-sonnet-4-20250514), constants
generators/
  base.py                   ClaudeClient wrapper, banned phrases, tolerant JSON parse
  brand_enrichment.py       homepage fetch + visible-text extract + GEO/ICP extraction
  post_gen.py               fan-out → cluster → generate → score → coverage/embedding gates
templates/index.html        single-page operator UI (Botric blue + navy, Inter, cards)
```

## Notes

- **Seed is optional** — a blank seed derives the anchor from the brand's most central
  use-case and still builds a full region cluster.
- **ICPs are storage-only** — detected, stored, editable, folded into the brand-context
  block; they do not add fan-out regions.
- **Graceful degradation** — a failed homepage fetch falls back to brand name + general
  knowledge; the embedding gate no-ops without an OpenAI key; the JSON parser strips
  markdown fences.
```
