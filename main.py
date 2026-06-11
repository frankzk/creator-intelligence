import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import (
    get_conn, get_creator_existing_ids, init_db, upsert_creator_analysis
)
from scraper import (
    download_video_audio, extract_username_from_url, scrape_profile_metadata
)
from transcriber import transcribe_and_cleanup
from analyzer import (
    analyze_creator_dna, analyze_product_patterns, analyze_product_video,
    classify_hook_type, generate_scripts, get_global_insights,
)
from kalodata import scrape_kalodata_sync
import videofactory as vf
import scriptstudio as ss

# ─── Init ─────────────────────────────────────────────────────────────────────

Path("uploads").mkdir(exist_ok=True)
Path("temp_audio").mkdir(exist_ok=True)

init_db()
vf.ensure_dirs()

app = FastAPI(title="Creator Intelligence", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/factory", StaticFiles(directory="factory"), name="factory")


@app.get("/")
async def root():
    return FileResponse("static/index.html")


# ─── Creators ─────────────────────────────────────────────────────────────────

class AddCreatorRequest(BaseModel):
    url: str
    display_name: str = ""


@app.get("/api/creators")
async def list_creators():
    conn = get_conn()
    rows = conn.execute("""
        SELECT c.*,
               (SELECT COUNT(*) FROM videos WHERE creator_id = c.id) AS video_count,
               ca.avg_views, ca.dominant_hook, ca.avg_duration,
               ca.top_hooks, ca.angles, ca.formula, ca.formula_steps,
               ca.updated_at AS analysis_updated
        FROM creators c
        LEFT JOIN creator_analyses ca ON ca.creator_id = c.id
        ORDER BY c.created_at DESC
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        for field in ("top_hooks", "angles", "formula_steps"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    d[field] = []
        result.append(d)
    return result


@app.post("/api/creators")
async def add_creator(req: AddCreatorRequest, background_tasks: BackgroundTasks):
    url = req.url.strip()
    username = extract_username_from_url(url)
    display_name = req.display_name.strip() or username

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO creators (username, display_name, url, status) VALUES (?,?,?,'scraping')",
            (username, display_name, url),
        )
        conn.commit()
    except Exception as exc:
        conn.close()
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(400, "El creador ya existe.")
        raise HTTPException(400, str(exc))

    creator_id = conn.execute(
        "SELECT id FROM creators WHERE username=?", (username,)
    ).fetchone()["id"]
    conn.close()

    background_tasks.add_task(_task_analyze_creator, creator_id, url, username, False)
    return {"id": creator_id, "username": username, "status": "scraping"}


@app.post("/api/creators/{creator_id}/refresh")
async def refresh_creator(creator_id: int, background_tasks: BackgroundTasks):
    conn = get_conn()
    creator = conn.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone()
    conn.close()
    if not creator:
        raise HTTPException(404, "Creator not found")
    background_tasks.add_task(
        _task_analyze_creator, creator_id, creator["url"], creator["username"], True
    )
    return {"status": "refreshing"}


@app.get("/api/creators/{creator_id}")
async def get_creator(creator_id: int):
    conn = get_conn()
    creator = conn.execute("SELECT * FROM creators WHERE id=?", (creator_id,)).fetchone()
    if not creator:
        conn.close()
        raise HTTPException(404, "Creator not found")

    analysis = conn.execute(
        "SELECT * FROM creator_analyses WHERE creator_id=?", (creator_id,)
    ).fetchone()
    video_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM videos WHERE creator_id=?", (creator_id,)
    ).fetchone()["cnt"]
    videos_1m = conn.execute(
        "SELECT COUNT(*) AS cnt FROM videos WHERE creator_id=? AND views >= 1000000", (creator_id,)
    ).fetchone()["cnt"]
    conn.close()

    result = dict(creator)
    result["video_count"] = video_count
    result["videos_1m"] = videos_1m

    if analysis:
        a = dict(analysis)
        for field in ("top_hooks", "angles", "formula_steps"):
            try:
                a[field] = json.loads(a.get(field) or "[]")
            except Exception:
                a[field] = []
        result["analysis"] = a

    return result


@app.get("/api/creators/{creator_id}/videos")
async def get_creator_videos(
    creator_id: int, sort: str = "views", page: int = 1, per_page: int = 10
):
    allowed = {"views": "views DESC", "duration": "duration DESC", "hook_type": "hook_type ASC"}
    order = allowed.get(sort, "views DESC")

    conn = get_conn()
    offset = (page - 1) * per_page
    videos = conn.execute(
        f"SELECT * FROM videos WHERE creator_id=? ORDER BY {order} LIMIT ? OFFSET ?",
        (creator_id, per_page, offset),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(*) AS cnt FROM videos WHERE creator_id=?", (creator_id,)
    ).fetchone()["cnt"]
    conn.close()

    return {"videos": [dict(v) for v in videos], "total": total, "page": page, "per_page": per_page}


@app.delete("/api/creators/{creator_id}")
async def delete_creator(creator_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM videos WHERE creator_id=?", (creator_id,))
    conn.execute("DELETE FROM creator_analyses WHERE creator_id=?", (creator_id,))
    conn.execute("DELETE FROM creators WHERE id=?", (creator_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ─── Products ─────────────────────────────────────────────────────────────────

class SearchProductRequest(BaseModel):
    keyword: str


@app.get("/api/products")
async def list_products():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM products ORDER BY analyzed_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/products/search")
async def search_product(req: SearchProductRequest, background_tasks: BackgroundTasks):
    keyword = req.keyword.strip()
    conn = get_conn()
    conn.execute("INSERT INTO products (keyword, status) VALUES (?, 'scraping')", (keyword,))
    conn.commit()
    product_id = conn.execute(
        "SELECT id FROM products WHERE keyword=? ORDER BY id DESC LIMIT 1", (keyword,)
    ).fetchone()["id"]
    conn.close()

    background_tasks.add_task(_task_analyze_product, product_id, keyword)
    return {"id": product_id, "keyword": keyword, "status": "scraping"}


@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    conn = get_conn()
    product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        conn.close()
        raise HTTPException(404, "Product not found")
    videos = conn.execute(
        "SELECT * FROM product_videos WHERE product_id=? ORDER BY gmv DESC", (product_id,)
    ).fetchall()
    conn.close()

    result = dict(product)
    result["videos"] = [dict(v) for v in videos]
    return result


@app.delete("/api/products/{product_id}")
async def delete_product(product_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM product_videos WHERE product_id=?", (product_id,))
    conn.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# ─── Scripts ──────────────────────────────────────────────────────────────────

class GenerateScriptsRequest(BaseModel):
    product_name: str
    benefit: Optional[str] = ""
    mode: str = "multicreador"
    creator_ids: list[int] = []
    product_ids: list[int] = []
    quantity: int = 3
    image_filename: Optional[str] = None


@app.post("/api/scripts/generate")
async def generate_scripts_endpoint(req: GenerateScriptsRequest):
    conn = get_conn()

    creators_data = []
    for cid in req.creator_ids:
        row = conn.execute("SELECT * FROM creators WHERE id=?", (cid,)).fetchone()
        if not row:
            continue
        c = dict(row)
        analysis = conn.execute(
            "SELECT * FROM creator_analyses WHERE creator_id=?", (cid,)
        ).fetchone()
        if analysis:
            a = dict(analysis)
            for field in ("top_hooks", "angles", "formula_steps"):
                try:
                    a[field] = json.loads(a.get(field) or "[]")
                except Exception:
                    a[field] = []
            c["analysis"] = a
        creators_data.append(c)

    products_data = []
    for pid in req.product_ids:
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if row:
            products_data.append(dict(row))

    conn.close()

    image_path = None
    if req.image_filename:
        candidate = os.path.join("uploads", req.image_filename)
        if os.path.exists(candidate):
            image_path = candidate

    # Run blocking Anthropic call in a thread so the event loop stays free.
    scripts = await asyncio.to_thread(
        generate_scripts,
        req.product_name,
        req.benefit or "",
        creators_data,
        products_data,
        req.mode,
        req.quantity,
        image_path,
    )
    return {"scripts": scripts}


# ─── Upload ───────────────────────────────────────────────────────────────────

@app.post("/api/upload")
async def upload_image(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise HTTPException(400, "Solo se aceptan imágenes JPG, PNG o WebP.")
    filename = f"{uuid.uuid4()}{ext}"
    filepath = os.path.join("uploads", filename)
    with open(filepath, "wb") as f:
        f.write(await file.read())
    return {"filename": filename, "url": f"/uploads/{filename}"}


# ─── Fábrica de creativos ─────────────────────────────────────────────────────

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


class CampaignRequest(BaseModel):
    name: str


@app.get("/api/factory/campaigns")
async def factory_list_campaigns(include_archived: bool = False):
    conn = get_conn()
    where = "" if include_archived else "WHERE ca.status='active'"
    rows = conn.execute(f"""
        SELECT ca.*,
               (SELECT COUNT(*) FROM factory_modules m WHERE m.campaign_id=ca.id) AS module_count,
               (SELECT COUNT(*) FROM factory_combos c WHERE c.campaign_id=ca.id AND c.status='ready') AS combo_count,
               (SELECT COUNT(*) FROM factory_combos c WHERE c.campaign_id=ca.id AND c.published_at IS NOT NULL) AS published_count
        FROM factory_campaigns ca {where} ORDER BY ca.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/factory/campaigns")
async def factory_create_campaign(req: CampaignRequest):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Nombre de campaña vacío")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO factory_campaigns (name) VALUES (?)", (name,))
        conn.commit()
    except Exception as exc:
        conn.close()
        if "UNIQUE" in str(exc):
            raise HTTPException(400, "Ya existe una campaña con ese nombre")
        raise
    row = conn.execute("SELECT * FROM factory_campaigns WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row)


class CampaignPatch(BaseModel):
    status: Optional[str] = None
    name: Optional[str] = None


@app.patch("/api/factory/campaigns/{campaign_id}")
async def factory_patch_campaign(campaign_id: int, req: CampaignPatch):
    conn = get_conn()
    if req.status in ("active", "archived"):
        conn.execute("UPDATE factory_campaigns SET status=? WHERE id=?",
                     (req.status, campaign_id))
    if req.name and req.name.strip():
        conn.execute("UPDATE factory_campaigns SET name=? WHERE id=?",
                     (req.name.strip(), campaign_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


# ─── Estudio de guiones ───────────────────────────────────────────────────────

class ResearchRequest(BaseModel):
    campaign_id: int
    urls: list[str] = []
    transcript: str = ""
    notes: str = ""


@app.post("/api/factory/research")
async def factory_add_research(req: ResearchRequest):
    conn = get_conn()
    if not conn.execute("SELECT id FROM factory_campaigns WHERE id=?",
                        (req.campaign_id,)).fetchone():
        conn.close()
        raise HTTPException(400, "Campaña inexistente")
    added = 0
    for url in req.urls:
        url = url.strip()
        if not url or not url.lower().startswith("http"):
            continue
        dup = conn.execute(
            "SELECT id FROM factory_research WHERE campaign_id=? AND url=?",
            (req.campaign_id, url)).fetchone()
        if dup:
            continue
        conn.execute(
            "INSERT INTO factory_research (campaign_id, url, notes) VALUES (?,?,?)",
            (req.campaign_id, url, req.notes.strip()))
        added += 1
    if req.transcript.strip():
        conn.execute("""
            INSERT INTO factory_research (campaign_id, transcript, notes, status)
            VALUES (?,?,?, 'done')
        """, (req.campaign_id, req.transcript.strip(), req.notes.strip()))
        added += 1
    conn.commit()
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM factory_research WHERE status='pending'"
    ).fetchone()["n"]
    conn.close()
    if not added:
        raise HTTPException(400, "No se agregó nada (¿links repetidos o vacíos?)")
    if pending:
        ss.kick_research()
    return {"added": added}


@app.get("/api/factory/research")
async def factory_list_research(campaign_id: int):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, url, notes, status, error, length(transcript) AS transcript_len "
        "FROM factory_research WHERE campaign_id=? ORDER BY id", (campaign_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/factory/research/{research_id}")
async def factory_delete_research(research_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM factory_research WHERE id=?", (research_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


class ScriptsGenRequest(BaseModel):
    campaign_id: int
    product_name: str = ""


@app.post("/api/factory/scripts/generate")
async def factory_generate_scripts(req: ScriptsGenRequest):
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM factory_research WHERE campaign_id=? AND status='done'",
        (req.campaign_id,)).fetchone()["n"]
    conn.close()
    if not n:
        raise HTTPException(400, "Primero transcribe al menos un video ganador (o pega una transcripción)")
    ss.kick_generation(req.campaign_id, req.product_name)
    return {"status": "generating", "sources": n}


@app.get("/api/factory/scripts")
async def factory_list_scripts(campaign_id: int):
    conn = get_conn()
    camp = conn.execute("SELECT angle_map FROM factory_campaigns WHERE id=?",
                        (campaign_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM factory_scripts WHERE campaign_id=? ORDER BY "
        "CASE type WHEN 'hook' THEN 0 WHEN 'body' THEN 1 ELSE 2 END, id",
        (campaign_id,)).fetchall()
    conn.close()
    try:
        angle_map = json.loads((camp["angle_map"] if camp else "") or "[]")
    except Exception:
        angle_map = []
    return {"angle_map": angle_map, "scripts": [dict(r) for r in rows]}


class ScriptPatch(BaseModel):
    status: str  # pending | recorded | discarded


@app.patch("/api/factory/scripts/{script_id}")
async def factory_patch_script(script_id: int, req: ScriptPatch):
    if req.status not in ("pending", "recorded", "discarded"):
        raise HTTPException(400, "status inválido")
    conn = get_conn()
    conn.execute("UPDATE factory_scripts SET status=? WHERE id=?",
                 (req.status, script_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/api/factory/modules")
async def factory_upload_module(
    file: UploadFile = File(...),
    type: str = Form(...),
    campaign_id: int = Form(...),
    tags: str = Form(""),
    overlay_text: str = Form(""),
    script_id: str = Form(""),
):
    if type not in vf.TYPE_PREFIX:
        raise HTTPException(400, "type debe ser hook, body o cta")
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, "Formato no soportado (mp4, mov, m4v, webm, mkv).")

    vf.ensure_dirs()
    conn = get_conn()
    if not conn.execute("SELECT id FROM factory_campaigns WHERE id=?", (campaign_id,)).fetchone():
        conn.close()
        raise HTTPException(400, "Campaña inexistente — crea una primero")
    label = vf.next_label(conn, type)
    src_path = vf.SRC_DIR / f"{label}{ext}"
    with open(src_path, "wb") as f:
        f.write(await file.read())

    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    overlay = overlay_text.strip()
    angle = ""
    sid = None
    if script_id.strip():
        srow = conn.execute("SELECT * FROM factory_scripts WHERE id=?",
                            (int(script_id),)).fetchone()
        if srow:
            sid = srow["id"]
            angle = srow["angle"] or ""
            if not overlay:
                overlay = srow["overlay_text"] or ""
            conn.execute("UPDATE factory_scripts SET status='recorded' WHERE id=?", (sid,))
    conn.execute("""
        INSERT INTO factory_modules
            (campaign_id, type, label, original_name, src_path, tags, overlay_text, script_id, angle)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (campaign_id, type, label, file.filename, str(src_path),
          json.dumps(tag_list, ensure_ascii=False), overlay, sid, angle))
    conn.commit()
    module_id = conn.execute(
        "SELECT id FROM factory_modules WHERE label=?", (label,)
    ).fetchone()["id"]
    conn.close()

    vf.kick_pipeline()
    return {"id": module_id, "label": label, "status": "uploaded"}


@app.get("/api/factory/modules")
async def factory_list_modules(campaign_id: Optional[int] = None):
    conn = get_conn()
    if campaign_id is not None:
        rows = conn.execute(
            "SELECT * FROM factory_modules WHERE campaign_id=? ORDER BY type, id",
            (campaign_id,),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM factory_modules ORDER BY type, id").fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d.pop("words", None)
        out.append(d)
    return out


class ModulePatch(BaseModel):
    overlay_text: Optional[str] = None
    tags: Optional[list[str]] = None


@app.patch("/api/factory/modules/{module_id}")
async def factory_patch_module(module_id: int, req: ModulePatch):
    conn = get_conn()
    row = conn.execute("SELECT * FROM factory_modules WHERE id=?", (module_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Módulo no encontrado")
    if req.tags is not None:
        conn.execute("UPDATE factory_modules SET tags=? WHERE id=?",
                     (json.dumps(req.tags, ensure_ascii=False), module_id))
    rerender = False
    if req.overlay_text is not None and req.overlay_text != row["overlay_text"]:
        # El overlay va horneado en el segmento → re-renderizar solo este módulo
        rerender = row["status"] in ("ready", "error") and bool(row["norm_path"])
        conn.execute(
            "UPDATE factory_modules SET overlay_text=?, status=CASE WHEN ? THEN 'transcribed' ELSE status END WHERE id=?",
            (req.overlay_text.strip(), rerender, module_id),
        )
    conn.commit()
    conn.close()
    if rerender:
        vf.kick_pipeline()
    return {"status": "ok", "rerender": rerender}


@app.delete("/api/factory/modules/{module_id}")
async def factory_delete_module(module_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM factory_modules WHERE id=?", (module_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Módulo no encontrado")
    combos = conn.execute(
        "SELECT output_path FROM factory_combos WHERE hook_id=? OR body_id=? OR cta_id=?",
        (module_id, module_id, module_id),
    ).fetchall()
    for c in combos:
        if c["output_path"]:
            Path(c["output_path"]).unlink(missing_ok=True)
    conn.execute(
        "DELETE FROM factory_combos WHERE hook_id=? OR body_id=? OR cta_id=?",
        (module_id, module_id, module_id),
    )
    conn.execute("DELETE FROM factory_modules WHERE id=?", (module_id,))
    conn.commit()
    conn.close()
    for p in (row["src_path"], row["norm_path"], row["seg_path"]):
        if p:
            Path(p).unlink(missing_ok=True)
    vf.write_manifest()
    return {"status": "deleted"}


class GenerateCombosRequest(BaseModel):
    campaign_id: int
    product_name: str = ""
    min_duration: float = 25.0
    max_duration: float = 40.0


@app.post("/api/factory/combos/generate")
async def factory_generate_combos(req: GenerateCombosRequest):
    conn = get_conn()
    counts = {t: conn.execute(
        "SELECT COUNT(*) AS n FROM factory_modules WHERE type=? AND status='ready' AND campaign_id=?",
        (t, req.campaign_id),
    ).fetchone()["n"] for t in ("hook", "body", "cta")}
    conn.close()
    missing = [t for t, n in counts.items() if n == 0]
    if missing:
        if vf.edits_running():
            raise HTTPException(400, "La IA está recortando módulos — las combinaciones se generarán solas al terminar")
        raise HTTPException(400, f"Faltan módulos listos de tipo: {', '.join(missing)}")
    vf.kick_combos(req.campaign_id, req.product_name, req.min_duration, req.max_duration)
    return {"status": "generating", "ready_modules": counts}


class EditModuleRequest(BaseModel):
    target_seconds: Optional[float] = None


@app.post("/api/factory/modules/{module_id}/edit")
async def factory_edit_module(module_id: int, req: EditModuleRequest):
    """Edición IA bajo demanda: silencios y tomas repetidas fuera; con
    target_seconds además ajusta la duración."""
    conn = get_conn()
    m = conn.execute("SELECT * FROM factory_modules WHERE id=?", (module_id,)).fetchone()
    conn.close()
    if not m:
        raise HTTPException(404, "Módulo no encontrado")
    if not m["norm_path"] or m["status"] not in ("ready", "transcribed", "error"):
        raise HTTPException(400, "El módulo aún se está procesando")
    if not json.loads(m["words"] or "[]"):
        raise HTTPException(400, "Sin transcripción palabra a palabra (¿clip sin voz?) — no se puede cortar por frases")
    if req.target_seconds is not None and req.target_seconds < 3:
        raise HTTPException(400, "Duración objetivo mínima: 3s")
    vf.kick_edit(module_id, req.target_seconds)
    return {"status": "editing", "label": m["label"]}


@app.get("/api/factory/combos")
async def factory_list_combos(campaign_id: Optional[int] = None):
    conn = get_conn()
    camp = "WHERE co.campaign_id=?" if campaign_id is not None else ""
    params = (campaign_id,) if campaign_id is not None else ()
    rows = conn.execute(f"""
        SELECT co.*, h.label AS hook_label, b.label AS body_label, c.label AS cta_label
        FROM factory_combos co
        JOIN factory_modules h ON h.id = co.hook_id
        JOIN factory_modules b ON b.id = co.body_id
        JOIN factory_modules c ON c.id = co.cta_id
        {camp}
        ORDER BY co.score DESC NULLS LAST, co.name
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/factory/combos")
async def factory_clear_combos(campaign_id: Optional[int] = None):
    conn = get_conn()
    camp = "WHERE campaign_id=?" if campaign_id is not None else ""
    params = (campaign_id,) if campaign_id is not None else ()
    rows = conn.execute(f"SELECT output_path FROM factory_combos {camp}", params).fetchall()
    conn.execute(f"DELETE FROM factory_combos {camp}", params)
    conn.commit()
    conn.close()
    for r in rows:
        if r["output_path"]:
            Path(r["output_path"]).unlink(missing_ok=True)
    vf.write_manifest()
    return {"status": "cleared", "deleted": len(rows)}


@app.post("/api/factory/combos/{combo_id}/published")
async def factory_mark_published(combo_id: int):
    conn = get_conn()
    cur = conn.execute(
        "UPDATE factory_combos SET published_at=CURRENT_TIMESTAMP WHERE id=?", (combo_id,)
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Combo no encontrado")
    vf.write_manifest()
    return {"status": "published"}


@app.get("/api/factory/queue")
async def factory_queue(n: int = 6, campaign_id: Optional[int] = None):
    """Cola del día: los mejores combos listos aún sin publicar."""
    conn = get_conn()
    camp = "AND campaign_id=?" if campaign_id is not None else ""
    params = (campaign_id, n) if campaign_id is not None else (n,)
    rows = conn.execute(f"""
        SELECT * FROM factory_combos
        WHERE status='ready' AND published_at IS NULL {camp}
        ORDER BY score DESC NULLS LAST, name LIMIT ?
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/factory/status")
async def factory_status():
    conn = get_conn()
    mod_counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM factory_modules GROUP BY status"
    ).fetchall()}
    combo_counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM factory_combos GROUP BY status"
    ).fetchall()}
    conn.close()
    return {
        "modules": mod_counts,
        "combos": combo_counts,
        "pipeline_running": vf.pipeline_running(),
        "combos_running": vf.combos_running(),
        "combos_note": vf.combos_note(),
        "research_running": ss.research_running(),
        "scripts_generating": ss.generation_running(),
        "scripts_error": ss.generation_error(),
        "edits_running": vf.edits_running(),
        "edit_note": vf.edit_note(),
    }


@app.get("/api/factory/manifest")
async def factory_manifest():
    path = vf.write_manifest()
    return FileResponse(path, filename="manifest.csv", media_type="text/csv")


@app.post("/api/factory/metrics")
async def factory_import_metrics(file: UploadFile = File(...)):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return vf.import_metrics_csv(text)


@app.get("/api/factory/attribution")
async def factory_attribution(campaign_id: Optional[int] = None):
    return vf.module_attribution(campaign_id)


# ─── Insights ─────────────────────────────────────────────────────────────────

@app.get("/api/insights")
async def global_insights():
    conn = get_conn()
    creators = conn.execute("SELECT * FROM creators WHERE status='done'").fetchall()

    all_data = []
    for c in creators:
        cd = dict(c)
        a = conn.execute(
            "SELECT * FROM creator_analyses WHERE creator_id=?", (c["id"],)
        ).fetchone()
        if a:
            ad = dict(a)
            for field in ("top_hooks", "angles", "formula_steps"):
                try:
                    ad[field] = json.loads(ad.get(field) or "[]")
                except Exception:
                    ad[field] = []
            cd["analysis"] = ad
        all_data.append(cd)

    conn.close()

    # Blocking Anthropic call → thread pool
    return await asyncio.to_thread(get_global_insights, all_data)


# ─── Background task implementations (sync, run in thread pool) ───────────────

def _set_creator_status(creator_id: int, status: str, niche: str | None = None):
    conn = get_conn()
    if niche is not None:
        conn.execute(
            "UPDATE creators SET status=?, niche=? WHERE id=?", (status, niche, creator_id)
        )
    else:
        conn.execute("UPDATE creators SET status=? WHERE id=?", (status, creator_id))
    conn.commit()
    conn.close()


def _sync_analyze_creator(creator_id: int, url: str, username: str, incremental: bool):
    conn = get_conn()
    try:
        _set_creator_status(creator_id, "scraping", "Extrayendo videos...")

        existing_ids = get_creator_existing_ids(creator_id) if incremental else set()
        videos_meta = scrape_profile_metadata(url, max_videos=25)
        new_videos = [v for v in videos_meta if v["tiktok_id"] not in existing_ids]

        if incremental and not new_videos:
            _set_creator_status(creator_id, "done")
            return

        _set_creator_status(creator_id, "transcribing", f"Transcribiendo {len(new_videos)} videos...")

        for v in new_videos:
            try:
                audio_path = download_video_audio(v["url"], "temp_audio")
                transcript = transcribe_and_cleanup(audio_path)
                hook_type = classify_hook_type(transcript, v.get("title", ""))
                conn.execute("""
                    INSERT OR IGNORE INTO videos
                        (creator_id, tiktok_id, title, views, likes, comments,
                         shares, duration, transcript, hook_type)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    creator_id, v["tiktok_id"], v.get("title", ""),
                    v.get("views", 0), v.get("likes", 0), v.get("comments", 0),
                    v.get("shares", 0), v.get("duration", 0), transcript, hook_type,
                ))
                conn.commit()
                v["transcript"] = transcript
            except Exception as exc:
                print(f"[creator/{username}] video error: {exc}")

        _set_creator_status(creator_id, "analyzing", "Analizando ADN...")

        all_videos = [dict(r) for r in conn.execute(
            "SELECT * FROM videos WHERE creator_id=? ORDER BY views DESC", (creator_id,)
        ).fetchall()]

        if all_videos:
            analysis = analyze_creator_dna(username, all_videos)
            upsert_creator_analysis(creator_id, analysis)
            conn.execute("""
                UPDATE creators SET
                    niche=?, video_count=?, last_scraped=CURRENT_TIMESTAMP, status='done'
                WHERE id=?
            """, (analysis.get("niche", "General"), len(all_videos), creator_id))
            conn.commit()
        else:
            _set_creator_status(creator_id, "done", "Sin videos")

    except Exception as exc:
        print(f"[creator/{username}] fatal: {exc}")
        _set_creator_status(creator_id, "error", f"Error: {str(exc)[:80]}")
    finally:
        conn.close()


def _sync_analyze_product(product_id: int, keyword: str):
    conn = get_conn()
    try:
        result = scrape_kalodata_sync(keyword)
        videos_meta = result.get("videos", [])

        conn.execute(
            "UPDATE products SET total_videos=?, total_gmv=?, status='transcribing' WHERE id=?",
            (len(videos_meta), result.get("total_gmv", 0), product_id),
        )
        conn.commit()

        transcribed = []
        for v in videos_meta:
            try:
                tiktok_url = v.get("tiktok_url", "")
                if not tiktok_url:
                    continue
                audio_path = download_video_audio(tiktok_url, "temp_audio")
                transcript = transcribe_and_cleanup(audio_path)
                why = analyze_product_video(transcript, v.get("views", 0), v.get("gmv", 0))
                conn.execute("""
                    INSERT INTO product_videos
                        (product_id, tiktok_url, creator_handle, views, gmv, transcript, why_it_converts)
                    VALUES (?,?,?,?,?,?,?)
                """, (
                    product_id, tiktok_url, v.get("creator_handle", ""),
                    v.get("views", 0), v.get("gmv", 0), transcript, why,
                ))
                conn.commit()
                v["transcript"] = transcript
                v["why_it_converts"] = why
                transcribed.append(v)
            except Exception as exc:
                print(f"[product/{keyword}] video error: {exc}")

        if transcribed:
            patterns = analyze_product_patterns(transcribed)
            conn.execute("""
                UPDATE products SET
                    top_hook=?, top_angle=?, ideal_duration=?,
                    analyzed_at=CURRENT_TIMESTAMP, status='done'
                WHERE id=?
            """, (
                patterns.get("top_hook", ""),
                patterns.get("top_angle", ""),
                patterns.get("ideal_duration", 45),
                product_id,
            ))
        else:
            conn.execute("UPDATE products SET status='done' WHERE id=?", (product_id,))
        conn.commit()

    except Exception as exc:
        print(f"[product/{keyword}] fatal: {exc}")
        conn.execute("UPDATE products SET status='error' WHERE id=?", (product_id,))
        conn.commit()
    finally:
        conn.close()


# Async wrappers so FastAPI's BackgroundTasks awaits them properly
# (sync functions called directly in BackgroundTasks run on the event loop thread)

async def _task_analyze_creator(creator_id: int, url: str, username: str, incremental: bool):
    await asyncio.to_thread(_sync_analyze_creator, creator_id, url, username, incremental)


async def _task_analyze_product(product_id: int, keyword: str):
    await asyncio.to_thread(_sync_analyze_product, product_id, keyword)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
