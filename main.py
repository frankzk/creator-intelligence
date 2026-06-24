import asyncio
import base64
import json
import os
import secrets
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
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
vf.kick_thumb_backfill()   # pósters JPG de módulos ya renderizados (hilo daemon, no bloquea)
vf.cleanup_tmp()           # borra trozos de subidas abandonadas (>24h)

app = FastAPI(title="Creator Intelligence", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/factory", StaticFiles(directory="factory"), name="factory")


# ─── Acceso (contraseña para publicar la app a internet de forma segura) ──────
# Si NO defines APP_PASSWORD en el .env → app abierta (uso local cómodo).
# Si la defines, se exige usuario+contraseña en TODA la app — imprescindible al
# exponerla por un túnel (Cloudflare) para que nadie más gaste tus créditos.
_APP_USER = os.getenv("APP_USER", "admin")
_APP_PASSWORD = os.getenv("APP_PASSWORD", "")


@app.middleware("http")
async def _password_gate(request: Request, call_next):
    if _APP_PASSWORD:
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
                ok = (secrets.compare_digest(user, _APP_USER)
                      and secrets.compare_digest(pwd, _APP_PASSWORD))
            except Exception:
                ok = False
        if not ok:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Fabrica de creativos"'},
            )
    return await call_next(request)


@app.get("/")
async def root():
    # no-cache: que el navegador siempre tome la última versión tras un git pull
    return FileResponse("static/index.html",
                        headers={"Cache-Control": "no-cache, must-revalidate"})


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


class CampaignFromPhoto(BaseModel):
    image_filename: str


@app.post("/api/factory/campaigns/from-photo")
async def factory_campaign_from_photo(req: CampaignFromPhoto):
    """Crea la campaña a partir de la foto: la IA le pone nombre y la foto queda guardada."""
    fn = (req.image_filename or "").strip()
    path = os.path.join("uploads", fn) if fn else ""
    if not fn or not os.path.exists(path):
        raise HTTPException(400, "Falta la foto del producto")
    name = ""
    try:
        from analyzer import name_product_from_photo
        name = (await asyncio.to_thread(name_product_from_photo, path) or "").strip()
    except Exception as exc:
        print(f"[campaign/name] {exc}")
    conn = get_conn()
    if not name:
        n = conn.execute("SELECT COUNT(*) AS n FROM factory_campaigns").fetchone()["n"]
        name = f"Producto {n + 1}"
    base, i = name, 2
    while conn.execute("SELECT id FROM factory_campaigns WHERE name=?", (name,)).fetchone():
        name = f"{base} {i}"
        i += 1
    conn.execute("INSERT INTO factory_campaigns (name, product_image, product_images) VALUES (?,?,?)",
                 (name, fn, json.dumps([fn])))
    conn.commit()
    row = conn.execute("SELECT * FROM factory_campaigns WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(row)


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
    product_image: Optional[str] = None   # foto activa (se añade a la galería)


@app.patch("/api/factory/campaigns/{campaign_id}")
async def factory_patch_campaign(campaign_id: int, req: CampaignPatch):
    conn = get_conn()
    if req.status in ("active", "archived"):
        conn.execute("UPDATE factory_campaigns SET status=? WHERE id=?",
                     (req.status, campaign_id))
    if req.name and req.name.strip():
        conn.execute("UPDATE factory_campaigns SET name=? WHERE id=?",
                     (req.name.strip(), campaign_id))
    if req.product_image and req.product_image.strip():
        pi = req.product_image.strip()
        row = conn.execute("SELECT product_images FROM factory_campaigns WHERE id=?",
                           (campaign_id,)).fetchone()
        try:
            gal = json.loads((row["product_images"] if row else "") or "[]")
        except Exception:
            gal = []
        if pi not in gal:
            gal.append(pi)
        conn.execute("UPDATE factory_campaigns SET product_image=?, product_images=? WHERE id=?",
                     (pi, json.dumps(gal, ensure_ascii=False), campaign_id))
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
            "SELECT id, status FROM factory_research WHERE campaign_id=? AND url=?",
            (req.campaign_id, url)).fetchone()
        if dup:
            # Reintento: si el link falló antes, vuelve a la cola en vez de ignorarse
            if dup["status"] == "error":
                conn.execute(
                    "UPDATE factory_research SET status='pending', error='' WHERE id=?",
                    (dup["id"],))
                added += 1
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


@app.get("/api/factory/research/{research_id}")
async def factory_get_research(research_id: int):
    conn = get_conn()
    row = conn.execute("SELECT * FROM factory_research WHERE id=?", (research_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "No encontrado")
    return dict(row)


@app.delete("/api/factory/research/{research_id}")
async def factory_delete_research(research_id: int):
    conn = get_conn()
    conn.execute("DELETE FROM factory_research WHERE id=?", (research_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


class PersonaResearchRequest(BaseModel):
    campaign_id: int


@app.post("/api/factory/personas/research")
async def factory_research_personas(req: PersonaResearchRequest):
    """La IA investiga (web + foto) y propone 2-3 buyer personas de voces reales."""
    conn = get_conn()
    camp = conn.execute("SELECT name, product_image FROM factory_campaigns WHERE id=?",
                        (req.campaign_id,)).fetchone()
    conn.close()
    if not camp:
        raise HTTPException(400, "Campaña inexistente")
    img = camp["product_image"] or ""
    path = os.path.join("uploads", img) if img else ""
    if not img or not os.path.exists(path):
        raise HTTPException(400, "Sube la foto del producto primero")
    ss.kick_persona_research(req.campaign_id, camp["name"], path)
    return {"status": "researching"}


class PersonaRemoveRequest(BaseModel):
    campaign_id: int
    persona: str


@app.post("/api/factory/personas/remove")
async def factory_remove_persona(req: PersonaRemoveRequest):
    """Quita una persona y todos sus guiones (p.ej. quedó de otro producto)."""
    conn = get_conn()
    conn.execute("DELETE FROM factory_scripts WHERE campaign_id=? AND persona=?",
                 (req.campaign_id, req.persona))
    row = conn.execute("SELECT personas, persona_candidates FROM factory_campaigns WHERE id=?",
                       (req.campaign_id,)).fetchone()

    def _prune(field):
        try:
            arr = json.loads((row[field] if row else "") or "[]")
        except Exception:
            arr = []
        return [p for p in arr if not (isinstance(p, dict) and (p.get("name") or "") == req.persona)]

    conn.execute("UPDATE factory_campaigns SET personas=?, persona_candidates=? WHERE id=?",
                 (json.dumps(_prune("personas"), ensure_ascii=False),
                  json.dumps(_prune("persona_candidates"), ensure_ascii=False), req.campaign_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


class PersonaRenameRequest(BaseModel):
    campaign_id: int
    old_name: str
    new_name: str


@app.post("/api/factory/personas/rename")
async def factory_rename_persona(req: PersonaRenameRequest):
    """Renombra una persona en toda la campaña (guiones, módulos, videos y la ficha)."""
    old = (req.old_name or "").strip()
    new = (req.new_name or "").strip()
    if not new:
        raise HTTPException(400, "El nombre no puede quedar vacío")
    if old == new:
        return {"status": "ok", "new": new}
    conn = get_conn()
    for tbl in ("factory_scripts", "factory_modules", "factory_combos"):
        conn.execute(f"UPDATE {tbl} SET persona=? WHERE campaign_id=? AND COALESCE(persona,'')=?",
                     (new, req.campaign_id, old))
    row = conn.execute("SELECT personas FROM factory_campaigns WHERE id=?",
                       (req.campaign_id,)).fetchone()
    try:
        personas = json.loads((row["personas"] if row else "") or "[]")
    except Exception:
        personas = []
    out, seen = [], set()
    for p in personas:
        if isinstance(p, dict):
            nm = (p.get("name") or "")
            if nm == old:
                p = {**p, "name": new}
                nm = new
            if nm in seen:
                continue          # ya existía una con el nombre nuevo → se fusiona
            seen.add(nm)
        out.append(p)
    conn.execute("UPDATE factory_campaigns SET personas=? WHERE id=?",
                 (json.dumps(out, ensure_ascii=False), req.campaign_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "new": new}


class PersonaMoveRequest(BaseModel):
    campaign_id: int            # origen
    persona: str
    to_campaign_id: int         # destino


@app.post("/api/factory/personas/move")
async def factory_move_persona(req: PersonaMoveRequest):
    """Mueve una persona y TODO lo suyo (guiones, módulos, videos) a otra campaña.
    Útil si una persona se generó en el producto equivocado."""
    persona = (req.persona or "").strip()
    if req.campaign_id == req.to_campaign_id:
        return {"status": "ok"}
    conn = get_conn()
    if not conn.execute("SELECT id FROM factory_campaigns WHERE id=?",
                        (req.to_campaign_id,)).fetchone():
        conn.close()
        raise HTTPException(400, "La campaña destino no existe")
    for tbl in ("factory_scripts", "factory_modules", "factory_combos"):
        conn.execute(f"UPDATE {tbl} SET campaign_id=? WHERE campaign_id=? AND COALESCE(persona,'')=?",
                     (req.to_campaign_id, req.campaign_id, persona))

    def _load(cid):
        r = conn.execute("SELECT personas FROM factory_campaigns WHERE id=?", (cid,)).fetchone()
        try:
            return json.loads((r["personas"] if r else "") or "[]")
        except Exception:
            return []

    src = _load(req.campaign_id)
    tgt = _load(req.to_campaign_id)
    moved = [p for p in src if isinstance(p, dict) and (p.get("name") or "") == persona]
    src = [p for p in src if not (isinstance(p, dict) and (p.get("name") or "") == persona)]
    tgt_names = {p.get("name") for p in tgt if isinstance(p, dict)}
    for p in moved:
        if p.get("name") not in tgt_names:
            tgt.append(p)
    conn.execute("UPDATE factory_campaigns SET personas=? WHERE id=?",
                 (json.dumps(src, ensure_ascii=False), req.campaign_id))
    conn.execute("UPDATE factory_campaigns SET personas=? WHERE id=?",
                 (json.dumps(tgt, ensure_ascii=False), req.to_campaign_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


class ScriptsGenRequest(BaseModel):
    campaign_id: int
    product_name: str = ""
    brief: str = ""              # avatar/ángulo opcional (1 buyer persona a la vez)
    image_filename: str = ""     # foto del producto (de /api/upload)


@app.post("/api/factory/scripts/generate")
async def factory_generate_scripts(req: ScriptsGenRequest):
    conn = get_conn()
    camp = conn.execute("SELECT product_image FROM factory_campaigns WHERE id=?",
                        (req.campaign_id,)).fetchone()
    if not camp:
        conn.close()
        raise HTTPException(400, "Campaña inexistente")
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM factory_research WHERE campaign_id=? AND status='done'",
        (req.campaign_id,)).fetchone()["n"]

    # Resolver la foto: la recién subida o la guardada en la campaña
    image_name = (req.image_filename or "").strip() or (camp["product_image"] or "")
    image_path = None
    if image_name:
        candidate = os.path.join("uploads", image_name)
        if os.path.exists(candidate):
            image_path = candidate
    if req.image_filename.strip():
        conn.execute("UPDATE factory_campaigns SET product_image=? WHERE id=?",
                     (req.image_filename.strip(), req.campaign_id))
        conn.commit()
    conn.close()

    if not image_path and not n:
        raise HTTPException(400, "Sube la foto del producto para generar guiones")

    ss.kick_generation(req.campaign_id, req.product_name, req.brief, image_path)
    return {"status": "generating", "sources": n, "has_image": bool(image_path)}


@app.get("/api/factory/scripts")
async def factory_list_scripts(campaign_id: int):
    conn = get_conn()
    camp = conn.execute(
        "SELECT angle_map, personas, persona_candidates FROM factory_campaigns WHERE id=?",
        (campaign_id,)).fetchone()
    rows = conn.execute(
        "SELECT * FROM factory_scripts WHERE campaign_id=? ORDER BY "
        "CASE type WHEN 'hook' THEN 0 WHEN 'body' THEN 1 ELSE 2 END, id",
        (campaign_id,)).fetchall()
    conn.close()

    def _j(field):
        try:
            return json.loads((camp[field] if camp else "") or "[]")
        except Exception:
            return []
    return {"angle_map": _j("angle_map"), "personas": _j("personas"),
            "persona_candidates": _j("persona_candidates"),
            "scripts": [dict(r) for r in rows]}


class ScriptPatch(BaseModel):
    status: Optional[str] = None       # pending | recorded | discarded
    text: Optional[str] = None         # editar el guion a mano
    overlay_text: Optional[str] = None # editar el texto en pantalla


@app.patch("/api/factory/scripts/{script_id}")
async def factory_patch_script(script_id: int, req: ScriptPatch):
    sets, params = [], []
    if req.status is not None:
        if req.status not in ("pending", "recorded", "discarded"):
            raise HTTPException(400, "status inválido")
        sets.append("status=?"); params.append(req.status)
    if req.text is not None:
        t = req.text.strip()
        if not t:
            raise HTTPException(400, "El guion no puede quedar vacío")
        sets.append("text=?"); params.append(t)
    if req.overlay_text is not None:
        sets.append("overlay_text=?"); params.append(req.overlay_text.strip())
    if not sets:
        raise HTTPException(400, "Nada que actualizar")
    conn = get_conn()
    row = conn.execute("SELECT id FROM factory_scripts WHERE id=?", (script_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Guion no encontrado")
    conn.execute(f"UPDATE factory_scripts SET {', '.join(sets)} WHERE id=?", (*params, script_id))
    conn.commit()
    conn.close()
    return {"status": "ok"}


class DoubleWinnerRequest(BaseModel):
    combo_id: int


@app.post("/api/factory/combos/double")
async def factory_double_winner(req: DoubleWinnerRequest):
    """Doblar al ganador: del combo que vendió, genera hooks nuevos del mismo
    ángulo para esa buyer persona (se añaden como pendientes)."""
    conn = get_conn()
    combo = conn.execute("SELECT * FROM factory_combos WHERE id=?", (req.combo_id,)).fetchone()
    if not combo:
        conn.close()
        raise HTTPException(404, "Combo no encontrado")
    hook = conn.execute("SELECT * FROM factory_modules WHERE id=?", (combo["hook_id"],)).fetchone()
    camp = conn.execute("SELECT personas, product_image FROM factory_campaigns WHERE id=?",
                        (combo["campaign_id"],)).fetchone()
    conn.close()
    if not hook:
        raise HTTPException(400, "El hook de este combo ya no existe")

    try:
        tags = json.loads(hook["tags"] or "[]")
    except Exception:
        tags = []
    try:
        personas = json.loads((camp["personas"] if camp else "") or "[]")
    except Exception:
        personas = []
    persona_names = {p.get("name") for p in personas if isinstance(p, dict)}
    persona = next((t for t in tags if t in persona_names), (tags[0] if tags else ""))
    if not persona:
        raise HTTPException(400, "Este combo no tiene buyer persona. Genera guiones por persona y vuelve a intentar.")
    pd = next((p for p in personas if isinstance(p, dict) and p.get("name") == persona), {})
    detail = " · ".join(filter(None, [
        f"dolor: {pd.get('pain')}" if pd.get("pain") else "",
        f"desea: {pd.get('desire')}" if pd.get("desire") else "",
        f"objeción: {pd.get('objection')}" if pd.get("objection") else ""]))
    winner_text = (hook["transcript"] or hook["overlay_text"] or "").strip()
    winner_angle = (hook["angle"] or "").strip()
    img = (camp["product_image"] if camp else "") or ""
    image_path = os.path.join("uploads", img) if img and os.path.exists(os.path.join("uploads", img)) else None

    ss.kick_winner_variations(combo["campaign_id"], persona, detail, winner_text, winner_angle, image_path)
    return {"status": "generating", "persona": persona}


@app.post("/api/factory/modules")
async def factory_upload_module(
    file: UploadFile = File(...),
    type: str = Form(...),
    campaign_id: int = Form(...),
    persona: str = Form(""),
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

    module_id = vf.create_module(
        conn, type=type, campaign_id=campaign_id, label=label,
        src_path=src_path, original_name=file.filename,
        persona=persona, overlay_text=overlay_text, script_id=script_id, tags=tags,
    )
    conn.close()
    vf.kick_pipeline()
    return {"id": module_id, "label": label, "status": "uploaded"}


# ─── Subida por trozos (esquiva el límite de ~100 MB del túnel) ──────────────

def _safe_upload_id(uid: str) -> str:
    uid = (uid or "").strip()
    if not (8 <= len(uid) <= 40) or any(c not in "0123456789abcdefABCDEF-" for c in uid):
        raise HTTPException(400, "upload_id inválido")
    return uid


@app.post("/api/factory/modules/chunk")
async def factory_upload_chunk(
    upload_id: str = Form(...),
    offset: int = Form(...),
    chunk: UploadFile = File(...),
):
    uid = _safe_upload_id(upload_id)
    if offset < 0:
        raise HTTPException(400, "offset inválido")
    vf.ensure_dirs()
    part = vf.TMP_DIR / f"{uid}.part"
    data = await chunk.read()
    if not part.exists():
        part.touch()
    # Escritura por offset → reintentar un trozo es idempotente (sobrescribe).
    with open(part, "r+b") as f:
        f.seek(offset)
        f.write(data)
    return {"ok": True, "offset": offset, "len": len(data)}


@app.post("/api/factory/modules/finalize")
async def factory_finalize_upload(
    upload_id: str = Form(...),
    filename: str = Form(...),
    type: str = Form(...),
    campaign_id: int = Form(...),
    size: int = Form(0),
    persona: str = Form(""),
    overlay_text: str = Form(""),
    script_id: str = Form(""),
    tags: str = Form(""),
):
    uid = _safe_upload_id(upload_id)
    if type not in vf.TYPE_PREFIX:
        raise HTTPException(400, "type debe ser hook, body o cta")
    ext = Path(filename or "").suffix.lower()
    if ext not in VIDEO_EXTS:
        raise HTTPException(400, "Formato no soportado (mp4, mov, m4v, webm, mkv).")
    part = vf.TMP_DIR / f"{uid}.part"
    if not part.exists():
        raise HTTPException(400, "No se recibieron los trozos — reintenta la subida")
    if size and part.stat().st_size != size:
        part.unlink(missing_ok=True)
        raise HTTPException(400, "Subida incompleta (faltaron trozos) — reintenta")

    conn = get_conn()
    if not conn.execute("SELECT id FROM factory_campaigns WHERE id=?", (campaign_id,)).fetchone():
        conn.close()
        part.unlink(missing_ok=True)
        raise HTTPException(400, "Campaña inexistente — crea una primero")
    label = vf.next_label(conn, type)
    src_path = vf.SRC_DIR / f"{label}{ext}"
    os.replace(part, src_path)          # TMP y SRC viven bajo factory/ → mismo FS, atómico
    module_id = vf.create_module(
        conn, type=type, campaign_id=campaign_id, label=label,
        src_path=src_path, original_name=filename,
        persona=persona, overlay_text=overlay_text, script_id=script_id, tags=tags,
    )
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
    persona: str = ""           # persona activa: solo combina sus módulos
    product_name: str = ""
    min_duration: float = 25.0
    max_duration: float = 40.0
    mode: str = "focused"       # focused = varía 1 pieza a la vez | all = todas


@app.post("/api/factory/combos/generate")
async def factory_generate_combos(req: GenerateCombosRequest):
    conn = get_conn()
    counts = {t: conn.execute(
        "SELECT COUNT(*) AS n FROM factory_modules "
        "WHERE type=? AND status='ready' AND campaign_id=? AND COALESCE(persona,'')=?",
        (t, req.campaign_id, req.persona or ""),
    ).fetchone()["n"] for t in ("hook", "body", "cta")}
    conn.close()
    missing = [t for t, n in counts.items() if n == 0]
    if missing:
        if vf.edits_running():
            raise HTTPException(400, "La IA está recortando módulos — las combinaciones se generarán solas al terminar")
        quien = f" para «{req.persona}»" if req.persona else ""
        raise HTTPException(400, f"Faltan módulos listos{quien} de tipo: {', '.join(missing)}")
    mode = req.mode if req.mode in ("focused", "all") else "focused"
    vf.kick_combos(req.campaign_id, req.persona, req.product_name,
                   req.min_duration, req.max_duration, mode)
    return {"status": "generating", "ready_modules": counts, "mode": mode}


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


class RerenderRequest(BaseModel):
    campaign_id: int


@app.post("/api/factory/rerender")
async def factory_rerender(req: RerenderRequest):
    """Re-renderiza todos los módulos listos con la plantilla actual
    (ej. tras cambiar captions/estilos). Los combos quedan obsoletos y se borran."""
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM factory_modules WHERE campaign_id=? AND status='ready'",
        (req.campaign_id,)).fetchone()["n"]
    if not n:
        conn.close()
        raise HTTPException(400, "No hay módulos listos para re-renderizar")
    for r in conn.execute("SELECT output_path FROM factory_combos WHERE campaign_id=?",
                          (req.campaign_id,)).fetchall():
        if r["output_path"]:
            Path(r["output_path"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM factory_combos WHERE campaign_id=?", (req.campaign_id,))
    conn.execute(
        "UPDATE factory_modules SET status='transcribed' WHERE campaign_id=? AND status='ready'",
        (req.campaign_id,))
    conn.commit()
    conn.close()
    vf.kick_pipeline()
    return {"rerendering": n}


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
        "personas_researching": ss.personas_researching(),
        "personas_error": ss.personas_error(),
        "api_key_present": bool(os.getenv("ANTHROPIC_API_KEY")),
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
