import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
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

# ─── Init ─────────────────────────────────────────────────────────────────────

Path("uploads").mkdir(exist_ok=True)
Path("temp_audio").mkdir(exist_ok=True)

init_db()

app = FastAPI(title="Creator Intelligence", version="1.0.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


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
