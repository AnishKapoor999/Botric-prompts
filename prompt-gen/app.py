"""Flask app: brand CRUD + enrich, async prompt generation, cluster ops, CSV export.

No auth. Single operator. Local only. SQLite in WAL mode so the background generation
thread and the UI can read concurrently.
"""
import csv
import io
import threading
import uuid

from flask import Flask, jsonify, render_template, request, Response

import db
from config import INTENT_TYPES, POST_BATCH_SIZES
from generators.brand_enrichment import enrich_brand
from generators.post_gen import PostGenerator, build_cluster_summary

app = Flask(__name__)

# Initialize the database at import time so the schema exists under any WSGI
# server (e.g. gunicorn on Railway), not just when run via `python app.py`.
# init_db() uses CREATE TABLE IF NOT EXISTS, so this is idempotent.
db.init_db()

# In-process background task registry: task_id -> {status, progress, message, result, error}
_TASKS = {}
_TASKS_LOCK = threading.Lock()


def _task_set(task_id, **fields):
    with _TASKS_LOCK:
        task = _TASKS.setdefault(task_id, {})
        task.update(fields)


def _task_get(task_id):
    with _TASKS_LOCK:
        task = _TASKS.get(task_id)
        return dict(task) if task else None


# ===========================================================================
# Brand body parsing
# ===========================================================================

_LIST_FIELDS = ("keywords", "use_cases", "pain_points", "features", "competitors")


def _as_list(val):
    """Accept a list, or a comma/newline-separated string, -> list of trimmed strings."""
    if val is None:
        return []
    if isinstance(val, list):
        items = val
    else:
        text = str(val)
        # split on newlines first, then commas
        parts = []
        for line in text.replace("\r", "\n").split("\n"):
            parts.extend(line.split(","))
        items = parts
    out = []
    for x in items:
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def _as_icps(val):
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
        pains = _as_list(item.get("pains"))
        if not any((label, role, segment, context, pains)):
            continue
        out.append({"label": label, "role": role, "segment": segment,
                    "context": context, "pains": pains})
    return out


def _parse_brand_body(data, partial=False):
    out = {}
    if "name" in data or not partial:
        out["name"] = (data.get("name") or "").strip()
    if "domain_url" in data or not partial:
        out["domain_url"] = (data.get("domain_url") or "").strip()
    if "context" in data or not partial:
        out["context"] = (data.get("context") or "").strip()
    if "category" in data or not partial:
        out["category"] = (data.get("category") or "").strip()
    if "audience" in data or not partial:
        out["audience"] = (data.get("audience") or "").strip()
    for f in _LIST_FIELDS:
        if f in data or not partial:
            out[f] = _as_list(data.get(f))
    if "icps" in data or not partial:
        out["icps"] = _as_icps(data.get("icps"))
    return out


# ===========================================================================
# Pages
# ===========================================================================

@app.route("/")
def index():
    return render_template("index.html")


# ===========================================================================
# Brand API
# ===========================================================================

@app.route("/api/brands", methods=["GET"])
def api_list_brands():
    return jsonify({"brands": db.list_brands()})


@app.route("/api/brands/<int:brand_id>", methods=["GET"])
def api_get_brand(brand_id):
    brand = db.get_brand(brand_id)
    if not brand:
        return jsonify({"error": "not found"}), 404
    return jsonify({"brand": brand})


@app.route("/api/brands", methods=["POST"])
def api_create_brand():
    data = request.get_json(force=True, silent=True) or {}
    body = _parse_brand_body(data, partial=False)
    if not body.get("name"):
        return jsonify({"error": "name is required"}), 400
    if not body.get("context"):
        return jsonify({"error": "context is required"}), 400
    if db.get_brand_by_name(body["name"]):
        return jsonify({"error": "a brand with that name already exists"}), 409
    brand = db.create_brand(body)
    return jsonify({"brand": brand}), 201


@app.route("/api/brands/<int:brand_id>", methods=["PUT"])
def api_update_brand(brand_id):
    if not db.get_brand(brand_id):
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    body = _parse_brand_body(data, partial=True)
    brand = db.update_brand(brand_id, body)
    return jsonify({"brand": brand})


@app.route("/api/brands/enrich", methods=["POST"])
def api_enrich_brand():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    domain_url = (data.get("domain_url") or "").strip()
    if not name and not domain_url:
        return jsonify({"error": "name or domain_url is required"}), 400
    draft = enrich_brand(name, domain_url)
    return jsonify({"draft": draft})


@app.route("/api/brands/<int:brand_id>/enrich", methods=["POST"])
def api_reenrich_brand(brand_id):
    brand = db.get_brand(brand_id)
    if not brand:
        return jsonify({"error": "not found"}), 404
    draft = enrich_brand(brand.get("name"), brand.get("domain_url"))
    return jsonify({"draft": draft})


@app.route("/api/brands/<int:brand_id>/personas/regenerate", methods=["POST"])
def api_regenerate_personas(brand_id):
    """Clear + rebuild the brand's buyer personas on demand."""
    brand = db.get_brand(brand_id)
    if not brand:
        return jsonify({"error": "not found"}), 404
    from generators.brand_enrichment import generate_brand_personas
    personas = generate_brand_personas(
        brand.get("name") or "", brand.get("domain_url") or "",
        category=brand.get("category"), audience=brand.get("audience"),
        use_cases=brand.get("use_cases"), pain_points=brand.get("pain_points"),
    )
    db.update_brand(brand_id, {"personas": personas})
    return jsonify({"personas": personas})


# ===========================================================================
# Prompt generation (async)
# ===========================================================================

def _run_generation(task_id, brand, params):
    def progress(msg, pct):
        _task_set(task_id, status="running", message=msg, progress=int(pct))

    try:
        gen = PostGenerator()
        result = gen.generate_posts(
            brands=[brand],
            count=params.get("count"),
            intent_counts=params.get("intent_counts"),
            seed=params.get("seed"),
            ai_search=params.get("ai_search", True),
            observed_queries=params.get("observed_queries"),
            target_rewrites=params.get("target_rewrites"),
            progress=progress,
        )
        _task_set(task_id, status="done", progress=100,
                  message="Done.", result=result, error=None)
    except Exception as e:
        import traceback
        traceback.print_exc()
        _task_set(task_id, status="error", message=str(e), error=str(e), result=None)


@app.route("/api/prompts/generate", methods=["POST"])
def api_generate():
    data = request.get_json(force=True, silent=True) or {}
    brand_id = data.get("brand_id")
    brand = db.get_brand(brand_id) if brand_id is not None else None
    if not brand:
        return jsonify({"error": "brand_id is required and must exist"}), 400

    count = data.get("count")
    if count is not None and count not in POST_BATCH_SIZES:
        return jsonify({"error": f"count must be one of {list(POST_BATCH_SIZES)}"}), 400

    params = {
        "seed": data.get("seed"),
        "count": count,
        "intent_counts": data.get("intent_counts"),
        "ai_search": data.get("ai_search", True),
        "observed_queries": data.get("observed_queries"),
        "target_rewrites": data.get("target_rewrites"),
    }

    task_id = uuid.uuid4().hex
    _task_set(task_id, status="queued", progress=0, message="Queued…",
              result=None, error=None)
    t = threading.Thread(target=_run_generation, args=(task_id, brand, params), daemon=True)
    t.start()
    return jsonify({"task_id": task_id})


@app.route("/api/tasks/<task_id>", methods=["GET"])
def api_task_status(task_id):
    task = _task_get(task_id)
    if not task:
        return jsonify({"error": "unknown task"}), 404
    return jsonify({
        "status": task.get("status"),
        "progress": task.get("progress", 0),
        "message": task.get("message"),
        "result": task.get("result"),
        "error": task.get("error"),
    })


# ===========================================================================
# Prompts (generated)
# ===========================================================================

@app.route("/api/prompts", methods=["GET"])
def api_list_prompts():
    brand_id = request.args.get("brand_id", type=int)
    intent = request.args.get("intent")
    posts = db.list_posts(brand_id=brand_id, intent=intent)
    return jsonify({"prompts": posts})


@app.route("/api/prompts/delete", methods=["POST"])
def api_delete_prompt():
    data = request.get_json(force=True, silent=True) or {}
    brand_id = data.get("brand_id")
    post_number = data.get("post_number")
    if brand_id is None or post_number is None:
        return jsonify({"error": "brand_id and post_number are required"}), 400
    try:
        n = db.delete_post(brand_id, int(post_number))
    except (TypeError, ValueError):
        return jsonify({"error": "post_number must be an integer"}), 400
    return jsonify({"deleted": n})


@app.route("/api/prompts/export.csv", methods=["GET"])
def api_export_csv():
    brand_id = request.args.get("brand_id", type=int)
    intent = request.args.get("intent")
    posts = db.list_posts(brand_id=brand_id, intent=intent)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["#", "brand", "intent", "region", "seed", "anchor",
                     "target_query", "title", "score"])
    for p in posts:
        writer.writerow([
            p.get("post_number"),
            p.get("brand_name") or "",
            p.get("intent") or "",
            p.get("region") or "",
            p.get("seed") or "",
            p.get("anchor") or "",
            p.get("target_query") or "",
            p.get("title") or "",
            p.get("ai_query_score") or 0,
        ])
    csv_bytes = buf.getvalue()
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=prompts.csv"},
    )


# ===========================================================================
# Clusters
# ===========================================================================

@app.route("/api/clusters", methods=["GET"])
def api_list_clusters():
    brand_id = request.args.get("brand_id", type=int)
    clusters = db.get_ai_search_clusters_for_brand(brand_id)

    # Anchor grounding per cluster, looked up from the brand's learned_context[seed_norm]
    # (each brand's JSON parsed once). None when a cluster predates the grounding feature.
    _learned = {}

    def _grounding(bid, seed_norm):
        if bid not in _learned:
            b = db.get_brand(bid) or {}
            lc = b.get("learned_context")
            _learned[bid] = lc if isinstance(lc, dict) else {}
        entry = _learned[bid].get(seed_norm)
        if isinstance(entry, dict) and (entry.get("summary") or "").strip():
            return {"summary": entry["summary"], "covers": bool(entry.get("covers", True))}
        return None

    out = []
    for c in clusters:
        summary = build_cluster_summary(c["brand_id"], c["seed_norm"])
        if summary:
            summary["grounding"] = _grounding(c["brand_id"], c["seed_norm"])
            out.append(summary)
    return jsonify({"clusters": out})


@app.route("/api/clusters/backfill", methods=["POST"])
def api_cluster_backfill():
    data = request.get_json(force=True, silent=True) or {}
    brand_id = data.get("brand_id")
    created = db.backfill_clusters_from_posts(brand_id)
    return jsonify({"created": created})


@app.route("/api/clusters/add-posts", methods=["POST"])
def api_cluster_add_posts():
    data = request.get_json(force=True, silent=True) or {}
    brand_id = data.get("brand_id")
    seed = data.get("seed") or ""
    post_numbers = data.get("post_numbers") or []
    try:
        post_numbers = [int(n) for n in post_numbers]
    except (TypeError, ValueError):
        return jsonify({"error": "post_numbers must be integers"}), 400
    seed_norm = db.normalize_seed(seed)
    result = db.attach_posts_to_cluster(brand_id, seed_norm, post_numbers)
    result["cluster"] = build_cluster_summary(brand_id, seed_norm)
    return jsonify(result)


@app.route("/api/clusters/add-fanout", methods=["POST"])
def api_cluster_add_fanout():
    data = request.get_json(force=True, silent=True) or {}
    brand_id = data.get("brand_id")
    seed = data.get("seed") or ""
    observed = data.get("observed_queries") or []
    seed_norm = db.normalize_seed(seed)
    cluster = db.get_ai_search_cluster(brand_id, seed_norm)
    if not cluster:
        return jsonify({"error": "cluster not found"}), 404

    gen = PostGenerator()
    merged = gen._merge_observed(cluster["rewrites"], observed)
    db.upsert_ai_search_cluster(
        brand_id, seed_norm, cluster.get("seed"), cluster.get("anchor"),
        merged["rewrites"], cluster.get("checklist") or [],
        backfilled=cluster.get("backfilled") or 0,
    )
    return jsonify({
        "added": merged["added"],
        "skipped": merged["skipped"],
        "cluster": build_cluster_summary(brand_id, seed_norm),
    })


@app.route("/api/clusters/delete", methods=["POST"])
def api_cluster_delete():
    data = request.get_json(force=True, silent=True) or {}
    brand_id = data.get("brand_id")
    seed = data.get("seed") or ""
    seed_norm = db.normalize_seed(seed)
    deleted_posts = 0
    if data.get("delete_posts"):
        deleted_posts = db.delete_posts_for_seed(brand_id, seed_norm)
    db.delete_ai_search_cluster(brand_id, seed_norm)
    return jsonify({"deleted": True, "deleted_posts": deleted_posts})


@app.route("/api/meta", methods=["GET"])
def api_meta():
    """Small helper for the UI: known intents & batch sizes."""
    return jsonify({
        "intents": list(INTENT_TYPES),
        "batch_sizes": list(POST_BATCH_SIZES),
    })


if __name__ == "__main__":
    import os
    db.init_db()
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="127.0.0.1", port=port, debug=debug, threaded=True)
