"""SQLite schema + all data access for the prompt-generation tool.

JSON columns are stored as text and parsed on read. All cluster rewrites are read
through `normalize_rewrites` so legacy/imported data still loads.
"""
import json
import os
import re
import sqlite3
import threading

from config import DB_PATH

# Serialize writes from the background generation thread and the request thread.
_write_lock = threading.Lock()


def _ensure_db_dir():
    """Create the parent directory of DB_PATH if it doesn't exist.

    Lets DB_PATH point at a mounted volume path (e.g. /data/strategy_bot.db on
    Railway) without requiring the directory to be pre-created.
    """
    d = os.path.dirname(DB_PATH)
    if d:
        os.makedirs(d, exist_ok=True)

# ---- JSON list/string column groups ----
_BRAND_LIST_FIELDS = (
    "keywords", "use_cases", "pain_points", "features", "competitors",
)
_BRAND_STR_FIELDS = ("name", "domain_url", "context", "category", "audience")


def _conn():
    _ensure_db_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _loads(raw, default):
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        val = json.loads(raw)
        return val if val is not None else default
    except (json.JSONDecodeError, TypeError):
        return default


def _dumps(val):
    return json.dumps(val if val is not None else [])


def init_db():
    """Create tables if they don't exist; run idempotent ALTERs for added columns."""
    with _write_lock, _conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS brands (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                domain_url   TEXT,
                context      TEXT NOT NULL,
                keywords     TEXT,
                added_at     TEXT DEFAULT (datetime('now')),
                UNIQUE(name)
            );

            CREATE TABLE IF NOT EXISTS posts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id        INTEGER REFERENCES brands(id),
                title           TEXT NOT NULL,
                intent          TEXT,
                ai_query_score  INTEGER DEFAULT 0,
                ai_search_meta  TEXT,
                concept_checklist TEXT,
                post_number     INTEGER,
                prompt_version  TEXT,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS ai_search_clusters (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                brand_id       INTEGER,
                seed_norm      TEXT NOT NULL,
                seed           TEXT,
                anchor         TEXT,
                rewrites_json  TEXT,
                checklist_json TEXT,
                backfilled     INTEGER DEFAULT 0,
                created_at     TEXT DEFAULT (datetime('now')),
                UNIQUE(brand_id, seed_norm)
            );
            """
        )
        # Idempotent enrichment columns on brands.
        brand_alter_cols = {
            "category": "TEXT",
            "audience": "TEXT",
            "use_cases": "TEXT",
            "pain_points": "TEXT",
            "features": "TEXT",
            "competitors": "TEXT",
            "enriched_at": "TEXT",
            "icps": "TEXT",
            "learned_context": "TEXT",
            "personas": "TEXT",
        }
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(brands)")}
        for col, typ in brand_alter_cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE brands ADD COLUMN {col} {typ}")


# ===========================================================================
# Brands
# ===========================================================================

def _brand_row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    for f in _BRAND_LIST_FIELDS:
        d[f] = _loads(d.get(f), [])
    d["icps"] = _loads(d.get("icps"), [])
    # Anchor-scoped grounding accumulator: JSON object keyed by seed_norm.
    d["learned_context"] = _loads(d.get("learned_context"), {})
    # Buyer personas (brand-level, JSON list).
    d["personas"] = _loads(d.get("personas"), [])
    # Convenience: count of prompts for this brand (filled by callers when needed).
    return d


def create_brand(data):
    """Insert a brand from a full brand body (see spec §4.4). Returns the new brand dict."""
    enriched_at = data.get("enriched_at")
    geo_fields = ("category", "audience", "use_cases", "pain_points", "features",
                  "competitors", "icps")
    if not enriched_at and any(data.get(f) for f in geo_fields):
        enriched_at = _now()
    with _write_lock, _conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO brands
              (name, domain_url, context, keywords, category, audience, use_cases,
               pain_points, features, competitors, icps, enriched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                (data.get("name") or "").strip(),
                (data.get("domain_url") or "").strip(),
                (data.get("context") or "").strip(),
                _dumps(data.get("keywords") or []),
                (data.get("category") or "").strip(),
                (data.get("audience") or "").strip(),
                _dumps(data.get("use_cases") or []),
                _dumps(data.get("pain_points") or []),
                _dumps(data.get("features") or []),
                _dumps(data.get("competitors") or []),
                _dumps(data.get("icps") or []),
                enriched_at,
            ),
        )
        brand_id = cur.lastrowid
    return get_brand(brand_id)


def update_brand(brand_id, data):
    """Partial update with the same body shape as create. Returns the updated brand."""
    existing = get_brand(brand_id)
    if not existing:
        return None
    # Recompute enriched_at if a GEO field is now populated.
    geo_fields = ("category", "audience", "use_cases", "pain_points", "features",
                  "competitors", "icps")
    enriched_at = data.get("enriched_at", existing.get("enriched_at"))
    if not enriched_at and any(data.get(f) for f in geo_fields):
        enriched_at = _now()

    fields, values = [], []

    def _set(col, val):
        fields.append(f"{col}=?")
        values.append(val)

    if "name" in data:
        _set("name", (data.get("name") or "").strip())
    if "domain_url" in data:
        _set("domain_url", (data.get("domain_url") or "").strip())
    if "context" in data:
        _set("context", (data.get("context") or "").strip())
    if "category" in data:
        _set("category", (data.get("category") or "").strip())
    if "audience" in data:
        _set("audience", (data.get("audience") or "").strip())
    for f in _BRAND_LIST_FIELDS:
        if f in data:
            _set(f, _dumps(data.get(f) or []))
    if "icps" in data:
        _set("icps", _dumps(data.get("icps") or []))
    if "learned_context" in data:
        # Stored verbatim as a JSON string; accept a dict too. Never touches `context`.
        lc = data.get("learned_context")
        _set("learned_context", lc if isinstance(lc, str) else json.dumps(lc or {}))
    if "personas" in data:
        # Stored verbatim as a JSON string; accept a list too.
        ps = data.get("personas")
        _set("personas", ps if isinstance(ps, str) else json.dumps(ps or []))
    _set("enriched_at", enriched_at)

    if not fields:
        return existing
    with _write_lock, _conn() as conn:
        conn.execute(
            f"UPDATE brands SET {', '.join(fields)} WHERE id=?",
            (*values, brand_id),
        )
    return get_brand(brand_id)


def get_brand(brand_id):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM brands WHERE id=?", (brand_id,)).fetchone()
    d = _brand_row_to_dict(row)
    if d:
        d["prompt_count"] = count_posts_for_brand(brand_id)
    return d


def get_brand_by_name(name):
    with _conn() as conn:
        row = conn.execute("SELECT * FROM brands WHERE name=?", (name,)).fetchone()
    return _brand_row_to_dict(row)


def list_brands():
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM brands ORDER BY added_at DESC, id DESC").fetchall()
    out = []
    for row in rows:
        d = _brand_row_to_dict(row)
        d["prompt_count"] = count_posts_for_brand(d["id"])
        out.append(d)
    return out


# ===========================================================================
# Posts (generated prompts)
# ===========================================================================

def count_posts_for_brand(brand_id):
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM posts WHERE brand_id=?", (brand_id,)
        ).fetchone()
    return row["n"] if row else 0


def _next_post_number(conn, brand_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(post_number), 0) AS m FROM posts WHERE brand_id=?",
        (brand_id,),
    ).fetchone()
    return (row["m"] or 0) + 1


def save_post(brand_id, post):
    """Persist one generated prompt; assigns a stable per-brand post_number."""
    with _write_lock, _conn() as conn:
        num = _next_post_number(conn, brand_id)
        cur = conn.execute(
            """
            INSERT INTO posts
              (brand_id, title, intent, ai_query_score, ai_search_meta,
               concept_checklist, post_number, prompt_version)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                brand_id,
                post.get("title") or "",
                post.get("intent"),
                int(post.get("ai_query_score") or 0),
                json.dumps(post.get("ai_search_meta") or {}),
                json.dumps(post.get("concept_checklist") or []),
                num,
                post.get("prompt_version"),
            ),
        )
        post_id = cur.lastrowid
    return post_id, num


def _post_row_to_dict(row):
    d = dict(row)
    d["ai_search_meta"] = _loads(d.get("ai_search_meta"), {})
    d["concept_checklist"] = _loads(d.get("concept_checklist"), [])
    meta = d["ai_search_meta"] if isinstance(d["ai_search_meta"], dict) else {}
    d["seed"] = meta.get("seed")
    d["anchor"] = meta.get("anchor")
    d["target_query"] = meta.get("target_query")
    d["region"] = meta.get("region")
    d["persona"] = meta.get("persona")
    return d


def list_posts(brand_id=None, intent=None):
    q = "SELECT p.*, b.name AS brand_name FROM posts p LEFT JOIN brands b ON b.id=p.brand_id"
    clauses, params = [], []
    if brand_id is not None:
        clauses.append("p.brand_id=?")
        params.append(brand_id)
    if intent:
        clauses.append("p.intent=?")
        params.append(intent)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY p.brand_id, p.post_number"
    with _conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [_post_row_to_dict(r) for r in rows]


def delete_post(brand_id, post_number):
    """Delete one generated prompt by its per-brand number. Returns rows deleted."""
    with _write_lock, _conn() as conn:
        cur = conn.execute(
            "DELETE FROM posts WHERE brand_id=? AND post_number=?",
            (brand_id, post_number),
        )
        return cur.rowcount


def delete_posts_for_seed(brand_id, seed_norm):
    """Delete all generated prompts belonging to a cluster (matched by normalized seed)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, ai_search_meta FROM posts WHERE brand_id=?", (brand_id,)
        ).fetchall()
    ids = []
    for r in rows:
        meta = _loads(r["ai_search_meta"], {})
        if isinstance(meta, dict) and normalize_seed(meta.get("seed") or "") == seed_norm:
            ids.append(r["id"])
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    with _write_lock, _conn() as conn:
        cur = conn.execute(f"DELETE FROM posts WHERE id IN ({marks})", ids)
        return cur.rowcount


def get_posts_by_numbers(brand_id, post_numbers):
    if not post_numbers:
        return []
    marks = ",".join("?" for _ in post_numbers)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM posts WHERE brand_id=? AND post_number IN ({marks})",
            (brand_id, *post_numbers),
        ).fetchall()
    return [_post_row_to_dict(r) for r in rows]


# ===========================================================================
# Clusters
# ===========================================================================

def normalize_seed(seed):
    """Lowercase/trim/collapse-whitespace key for a seed (blank seed -> '')."""
    if not seed:
        return ""
    return re.sub(r"\s+", " ", str(seed).strip().lower())


def normalize_rewrites(raw):
    """Backward-compatible reader: JSON string|list, legacy [str] or [{query,region,source}].

    Returns the object form, deduped by query (case-insensitive).
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = []
    out, seen = [], set()
    for r in (raw or []):
        if isinstance(r, dict):
            q = (r.get("query") or "").strip()
            region = r.get("region") or "(unsorted)"
            source = r.get("source") or "generated"
            persona = (r.get("persona") or "").strip()   # legacy -> ""
        else:
            q = str(r).strip()
            region, source, persona = "(unsorted)", "generated", ""
        k = q.lower()
        if q and k not in seen:
            seen.add(k)
            out.append({"query": q, "region": region, "source": source,
                        "persona": persona})
    return out


# --- Tolerant query <-> rewrite matching (paraphrase-safe coverage) ---------
_MATCH_STOPWORDS = {
    "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "is", "are", "do",
    "does", "my", "i", "what", "whats", "which", "best", "how", "when", "where", "with",
    "that", "this", "it", "you", "your", "get", "getting", "as", "at", "be", "can",
    "vs", "versus", "into", "by", "should", "use", "using", "good", "any",
}


def _norm_query(q):
    s = re.sub(r"[^\w\s]", " ", (q or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _content_tokens(q):
    return {t for t in _norm_query(q).split() if t and t not in _MATCH_STOPWORDS}


def match_query_to_rewrites(target_query, rewrite_queries):
    """Best canonical rewrite query for `target_query`, or None.

    Normalized-exact first; else content-token overlap requiring >=2 shared tokens,
    scoring max(Jaccard, overlap-coefficient), and returning the best rewrite scoring
    >= 0.5. Tolerant of paraphrase, conservative against mis-binding.
    """
    if not target_query or not rewrite_queries:
        return None
    tq_norm = _norm_query(target_query)
    if tq_norm:
        for rq in rewrite_queries:
            if _norm_query(rq) == tq_norm:
                return rq
    tq_tokens = _content_tokens(target_query)
    if not tq_tokens:
        return None
    best, best_score = None, 0.0
    for rq in rewrite_queries:
        rq_tokens = _content_tokens(rq)
        if not rq_tokens:
            continue
        shared = tq_tokens & rq_tokens
        if len(shared) < 2:
            continue
        union = tq_tokens | rq_tokens
        jaccard = len(shared) / len(union) if union else 0.0
        overlap = len(shared) / min(len(tq_tokens), len(rq_tokens))
        score = max(jaccard, overlap)
        if score > best_score:
            best, best_score = rq, score
    return best if best_score >= 0.5 else None


def get_ai_search_cluster(brand_id, seed_norm):
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM ai_search_clusters WHERE brand_id=? AND seed_norm=?",
            (brand_id, seed_norm),
        ).fetchone()
    return _cluster_row_to_dict(row)


def get_ai_search_clusters_for_brand(brand_id=None):
    q = "SELECT * FROM ai_search_clusters"
    params = []
    if brand_id is not None:
        q += " WHERE brand_id=?"
        params.append(brand_id)
    q += " ORDER BY brand_id, created_at DESC, id DESC"
    with _conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [_cluster_row_to_dict(r) for r in rows]


def _cluster_row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    d["rewrites"] = normalize_rewrites(d.get("rewrites_json"))
    d["checklist"] = _loads(d.get("checklist_json"), [])
    return d


def save_ai_search_cluster(brand_id, seed_norm, seed, anchor, rewrites, checklist,
                           backfilled=0):
    """INSERT OR IGNORE — does not overwrite an existing cluster."""
    with _write_lock, _conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO ai_search_clusters
              (brand_id, seed_norm, seed, anchor, rewrites_json, checklist_json, backfilled)
            VALUES (?,?,?,?,?,?,?)
            """,
            (brand_id, seed_norm, seed, anchor,
             json.dumps(rewrites or []), json.dumps(checklist or []), backfilled),
        )
    return get_ai_search_cluster(brand_id, seed_norm)


def upsert_ai_search_cluster(brand_id, seed_norm, seed, anchor, rewrites, checklist,
                             backfilled=0):
    """UPDATE if exists else INSERT."""
    with _write_lock, _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM ai_search_clusters WHERE brand_id=? AND seed_norm=?",
            (brand_id, seed_norm),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE ai_search_clusters
                   SET seed=?, anchor=?, rewrites_json=?, checklist_json=?, backfilled=?
                 WHERE brand_id=? AND seed_norm=?
                """,
                (seed, anchor, json.dumps(rewrites or []), json.dumps(checklist or []),
                 backfilled, brand_id, seed_norm),
            )
        else:
            conn.execute(
                """
                INSERT INTO ai_search_clusters
                  (brand_id, seed_norm, seed, anchor, rewrites_json, checklist_json, backfilled)
                VALUES (?,?,?,?,?,?,?)
                """,
                (brand_id, seed_norm, seed, anchor,
                 json.dumps(rewrites or []), json.dumps(checklist or []), backfilled),
            )
    return get_ai_search_cluster(brand_id, seed_norm)


def delete_ai_search_cluster(brand_id, seed_norm):
    with _write_lock, _conn() as conn:
        conn.execute(
            "DELETE FROM ai_search_clusters WHERE brand_id=? AND seed_norm=?",
            (brand_id, seed_norm),
        )


def get_covered_target_queries(brand_id, seed_norm):
    """Set of target_query values already used by generated prompts for this cluster."""
    covered = set()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT ai_search_meta FROM posts WHERE brand_id=?", (brand_id,)
        ).fetchall()
    for r in rows:
        meta = _loads(r["ai_search_meta"], {})
        if not isinstance(meta, dict):
            continue
        if normalize_seed(meta.get("seed") or "") != seed_norm:
            continue
        tq = (meta.get("target_query") or "").strip()
        if tq:
            covered.add(tq)
    return covered


def get_post_numbers_by_target_query(brand_id, seed_norm):
    """Map target_query (lower) -> [post_number] for coverage linking in the cluster view."""
    mapping = {}
    with _conn() as conn:
        rows = conn.execute(
            "SELECT post_number, ai_search_meta FROM posts WHERE brand_id=?", (brand_id,)
        ).fetchall()
    for r in rows:
        meta = _loads(r["ai_search_meta"], {})
        if not isinstance(meta, dict):
            continue
        if normalize_seed(meta.get("seed") or "") != seed_norm:
            continue
        tq = (meta.get("target_query") or "").strip().lower()
        if tq:
            mapping.setdefault(tq, []).append(r["post_number"])
    return mapping


def backfill_clusters_from_posts(brand_id=None):
    """Reconstruct cluster rows from previously generated prompts.

    rewrites = covered-only (source="manual"), backfilled=1.
    """
    q = "SELECT brand_id, ai_search_meta FROM posts"
    params = []
    if brand_id is not None:
        q += " WHERE brand_id=?"
        params.append(brand_id)
    with _conn() as conn:
        rows = conn.execute(q, params).fetchall()

    # Group covered queries by (brand_id, seed_norm).
    groups = {}
    for r in rows:
        meta = _loads(r["ai_search_meta"], {})
        if not isinstance(meta, dict):
            continue
        tq = (meta.get("target_query") or "").strip()
        if not tq:
            continue
        seed = meta.get("seed") or ""
        seed_norm = normalize_seed(seed)
        key = (r["brand_id"], seed_norm)
        groups.setdefault(key, {"seed": seed, "anchor": meta.get("anchor"), "queries": []})
        if tq not in groups[key]["queries"]:
            groups[key]["queries"].append(tq)
            if meta.get("anchor") and not groups[key]["anchor"]:
                groups[key]["anchor"] = meta.get("anchor")

    created = 0
    for (bid, seed_norm), info in groups.items():
        existing = get_ai_search_cluster(bid, seed_norm)
        if existing:
            continue
        rewrites = [{"query": q, "region": "(unsorted)", "source": "manual"}
                    for q in info["queries"]]
        save_ai_search_cluster(bid, seed_norm, info["seed"], info["anchor"],
                               rewrites, [], backfilled=1)
        created += 1
    return created


def attach_posts_to_cluster(brand_id, seed_norm, post_numbers, region_by_num=None):
    """Fold existing prompts (by per-brand number) into a cluster.

    Re-stamps each post's ai_search_meta seed to the cluster seed so coverage picks it
    up. When `region_by_num` ({post_number: region}) is supplied, each prompt is routed
    to the right region:
      - region matches an EXISTING cluster region -> the prompt COVERS it (its
        target_query is set to that region's primary query);
      - else -> a NEW manual rewrite is added {query: title, region: <classified or
        "(from posts)">, source: "manual", persona: ""} and the prompt targets it.
    Without `region_by_num`, the prior "(unsorted)" behavior is kept.
    """
    cluster = get_ai_search_cluster(brand_id, seed_norm)
    if not cluster:
        return {"attached": [], "missing": list(post_numbers), "error": "cluster not found"}

    posts = get_posts_by_numbers(brand_id, post_numbers)
    found_nums = {p["post_number"] for p in posts}
    missing = [n for n in post_numbers if n not in found_nums]

    rewrites = list(cluster["rewrites"])
    existing_queries = {(r.get("query") or "").strip().lower() for r in rewrites}
    # region label (lowercased) -> the region's primary rewrite query
    region_to_query = {}
    for r in rewrites:
        rk = (r.get("region") or "").strip().lower()
        if rk and rk not in region_to_query:
            region_to_query[rk] = (r.get("query") or "").strip()

    region_by_num = region_by_num or {}
    attached, existing, new = [], [], []

    with _write_lock, _conn() as conn:
        for p in posts:
            num = p["post_number"]
            meta = p.get("ai_search_meta") or {}
            if not isinstance(meta, dict):
                meta = {}
            title = (p.get("title") or "").strip()
            classified = (region_by_num.get(num) or region_by_num.get(str(num)) or "").strip()
            ck = classified.lower()

            if classified and ck in region_to_query:
                # Covers an existing region: target its primary query.
                tq = region_to_query[ck]
                existing.append(num)
            elif classified:
                # New region for this prompt.
                tq = title
                if tq and tq.lower() not in existing_queries:
                    rewrites.append({"query": tq, "region": classified,
                                     "source": "manual", "persona": ""})
                    existing_queries.add(tq.lower())
                    region_to_query.setdefault(ck, tq)
                new.append(num)
            else:
                # No classification supplied -> legacy behavior.
                tq = (meta.get("target_query") or title).strip()
                if tq and tq.lower() not in existing_queries:
                    rewrites.append({"query": tq, "region": "(from posts)",
                                     "source": "manual", "persona": ""})
                    existing_queries.add(tq.lower())

            meta["seed"] = cluster["seed"] or ""
            meta["target_query"] = tq
            if not meta.get("anchor"):
                meta["anchor"] = cluster.get("anchor")
            conn.execute(
                "UPDATE posts SET ai_search_meta=? WHERE brand_id=? AND post_number=?",
                (json.dumps(meta), brand_id, num),
            )
            attached.append(num)

    upsert_ai_search_cluster(brand_id, seed_norm, cluster["seed"], cluster.get("anchor"),
                             rewrites, cluster.get("checklist") or [],
                             backfilled=cluster.get("backfilled") or 0)
    return {"attached": attached, "missing": missing, "existing": existing,
            "new": new, "cluster_size": len(rewrites)}


def _now():
    """ISO-ish UTC timestamp (delegated to SQLite to avoid clock-dependent test breakage)."""
    with _conn() as conn:
        row = conn.execute("SELECT datetime('now') AS t").fetchone()
    return row["t"]
