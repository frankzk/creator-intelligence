"""Fábrica de creativos: normaliza módulos (hook/cuerpo/CTA), los renderiza
con Remotion (captions karaoke + texto overlay) y arma todas las combinaciones
H×B×C con ffmpeg concat sin re-encodear.

Costo de render: crece con los MÓDULOS, no con las combinaciones — cada combo
es un concat -c copy (<1s). Requiere que todos los segmentos salgan del mismo
pipeline de Remotion con parámetros idénticos (1080x1920@30, h264+aac).
"""
import csv
import json
import os
import re
import shutil
import subprocess
import threading
from itertools import product as cartesian
from pathlib import Path

from database import get_conn

FACTORY_DIR = Path("factory")
SRC_DIR = FACTORY_DIR / "src"            # originales subidos
NORM_DIR = FACTORY_DIR / "normalized"    # 1080x1920@30 normalizados
SEG_DIR = FACTORY_DIR / "segments"       # renderizados por Remotion
OUT_DIR = FACTORY_DIR / "output"         # combos finales
RENDER_DIR = Path("render")
RENDER_INPUTS = RENDER_DIR / "public" / "inputs"

TYPE_PREFIX = {"hook": "H", "body": "B", "cta": "C"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def ensure_dirs():
    for d in (SRC_DIR, NORM_DIR, SEG_DIR, OUT_DIR, RENDER_INPUTS):
        d.mkdir(parents=True, exist_ok=True)


# ─── ffmpeg helpers ───────────────────────────────────────────────────────────

def _ffmpeg() -> str:
    cand = os.getenv("FFMPEG_BIN") or shutil.which("ffmpeg")
    if cand:
        return cand
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError(
            "ffmpeg no encontrado. Instálalo (https://ffmpeg.org) o `pip install imageio-ffmpeg`."
        )


def _run(cmd: list[str]):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} falló: {proc.stderr[-400:]}")
    return proc


def media_duration(path: str | Path) -> float:
    ffprobe = os.getenv("FFPROBE_BIN") or shutil.which("ffprobe")
    if ffprobe:
        proc = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
        )
        try:
            return float(proc.stdout.strip())
        except ValueError:
            pass
    # Sin ffprobe (p.ej. imageio-ffmpeg): parsear el banner de ffmpeg
    proc = subprocess.run([_ffmpeg(), "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", proc.stderr)
    if not m:
        raise RuntimeError(f"No pude leer la duración de {path}")
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def _has_audio(path: str | Path) -> bool:
    proc = subprocess.run([_ffmpeg(), "-i", str(path)], capture_output=True, text=True)
    return "Audio:" in proc.stderr


def normalize(src: str | Path, dst: str | Path):
    """Lleva cualquier clip a 1080x1920@30 h264 + aac 48kHz estéreo -14 LUFS.
    Si el clip no trae audio (b-roll mudo) se inyecta una pista en silencio
    para que el concat posterior no se rompa."""
    vf = ("scale=1080:1920:force_original_aspect_ratio=decrease,"
          "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,format=yuv420p,setsar=1")
    common = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
              "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
              "-movflags", "+faststart", str(dst)]
    if _has_audio(src):
        cmd = [_ffmpeg(), "-y", "-i", str(src), "-vf", vf,
               "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"] + common
    else:
        cmd = [_ffmpeg(), "-y", "-i", str(src),
               "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
               "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-vf", vf] + common
    _run(cmd)


def concat(segments: list[str | Path], dst: str | Path):
    """Concatena segmentos ya homogéneos sin re-encodear."""
    list_file = Path(dst).with_suffix(".txt")
    list_file.write_text(
        "".join(f"file '{Path(s).resolve()}'\n" for s in segments), encoding="utf-8"
    )
    try:
        _run([_ffmpeg(), "-y", "-f", "concat", "-safe", "0",
              "-i", str(list_file), "-c", "copy", "-movflags", "+faststart", str(dst)])
    finally:
        list_file.unlink(missing_ok=True)


# ─── Pipeline de módulos (normalizar → transcribir → renderizar) ─────────────

_pipeline_lock = threading.Lock()
_pipeline_running = False
_combos_lock = threading.Lock()
_combos_running = False


def pipeline_running() -> bool:
    return _pipeline_running


def combos_running() -> bool:
    return _combos_running


def next_label(conn, mtype: str) -> str:
    """H1, H2... por tipo, sin reusar números tras borrar."""
    prefix = TYPE_PREFIX[mtype]
    rows = conn.execute(
        "SELECT label FROM factory_modules WHERE type=?", (mtype,)
    ).fetchall()
    top = 0
    for r in rows:
        m = re.fullmatch(rf"{prefix}(\d+)", r["label"])
        if m:
            top = max(top, int(m.group(1)))
    return f"{prefix}{top + 1}"


def kick_pipeline():
    """Arranca el worker de módulos si no está corriendo (idempotente)."""
    global _pipeline_running
    with _pipeline_lock:
        if _pipeline_running:
            return
        _pipeline_running = True
    threading.Thread(target=_pipeline_worker, daemon=True).start()


def _set_module(conn, module_id: int, **fields):
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE factory_modules SET {cols} WHERE id=?",
                 (*fields.values(), module_id))
    conn.commit()


def _pipeline_worker():
    global _pipeline_running
    try:
        while True:
            worked = _prepare_pending_modules()
            worked = _render_transcribed_modules() or worked
            if not worked:
                break
    finally:
        global _pipeline_running
        with _pipeline_lock:
            _pipeline_running = False


def _prepare_pending_modules() -> bool:
    """uploaded → normalizado + transcrito → 'transcribed'."""
    from transcriber import transcribe_words  # import perezoso: carga el modelo whisper

    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM factory_modules WHERE status='uploaded' ORDER BY id"
    ).fetchall()
    worked = False
    for m in rows:
        worked = True
        try:
            _set_module(conn, m["id"], status="normalizing")
            norm_path = NORM_DIR / f"{m['label']}.mp4"
            normalize(m["src_path"], norm_path)
            duration = media_duration(norm_path)

            _set_module(conn, m["id"], status="transcribing",
                        norm_path=str(norm_path), duration=round(duration, 3))
            # Transcripción no-fatal: sin whisper el módulo sale sin captions
            transcript, words, warn = "", [], ""
            try:
                transcript, words = transcribe_words(str(norm_path), language="es")
            except Exception as exc:
                warn = f"sin captions (whisper falló): {str(exc)[:200]}"
                print(f"[factory/{m['label']}] {warn}")
            _set_module(conn, m["id"], status="transcribed", transcript=transcript,
                        words=json.dumps(words, ensure_ascii=False), error=warn)
        except Exception as exc:
            print(f"[factory/{m['label']}] error preparando: {exc}")
            _set_module(conn, m["id"], status="error", error=str(exc)[:300])
    conn.close()
    return worked


def _render_transcribed_modules() -> bool:
    """transcribed → segmento Remotion → 'ready'. Un solo bundle por tanda."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM factory_modules WHERE status='transcribed' ORDER BY id"
    ).fetchall()
    if not rows:
        conn.close()
        return False

    ensure_dirs()
    jobs = []
    for m in rows:
        # Remotion solo sirve archivos de public/ → copiar el normalizado ahí
        input_name = f"{m['label']}.mp4"
        shutil.copy2(m["norm_path"], RENDER_INPUTS / input_name)
        seg_path = SEG_DIR / f"{m['label']}.mp4"
        jobs.append({
            "id": m["id"],
            "out": str(seg_path.resolve()),
            "props": {
                "srcName": f"inputs/{input_name}",
                "durationInSeconds": m["duration"],
                "words": json.loads(m["words"] or "[]"),
                "overlayText": m["overlay_text"] or "",
                "moduleType": m["type"],
            },
        })
        _set_module(conn, m["id"], status="rendering")

    jobs_file = FACTORY_DIR / "render_jobs.json"
    jobs_file.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False), encoding="utf-8")

    try:
        proc = subprocess.run(
            ["node", "render.mjs", str(jobs_file.resolve())],
            cwd=str(RENDER_DIR), capture_output=True, text=True, timeout=3600,
        )
        ok_ids = set()
        for line in proc.stdout.splitlines():
            try:
                msg = json.loads(line)
                if "done" in msg:
                    ok_ids.add(msg["done"])
            except json.JSONDecodeError:
                continue
        for m in rows:
            if m["id"] in ok_ids:
                _set_module(conn, m["id"], status="ready",
                            seg_path=str(SEG_DIR / f"{m['label']}.mp4"))
            else:
                detail = proc.stderr[-300:] if proc.returncode != 0 else "render incompleto"
                _set_module(conn, m["id"], status="error", error=detail)
    except Exception as exc:
        print(f"[factory/render] error: {exc}")
        for m in rows:
            _set_module(conn, m["id"], status="error", error=str(exc)[:300])
    finally:
        for j in jobs:
            (RENDER_INPUTS / Path(j["props"]["srcName"]).name).unlink(missing_ok=True)

    conn.close()
    return True


# ─── Matriz de combinaciones ──────────────────────────────────────────────────

def _tags_compatible(*tag_lists: list[str]) -> bool:
    """Módulos sin tags combinan con todo; con tags, cada par debe compartir una."""
    tagged = [set(t) for t in tag_lists if t]
    return all(a & b for i, a in enumerate(tagged) for b in tagged[i + 1:])


def kick_combos(product_name: str, min_dur: float, max_dur: float):
    global _combos_running
    with _combos_lock:
        if _combos_running:
            return
        _combos_running = True
    threading.Thread(
        target=_combos_worker, args=(product_name, min_dur, max_dur), daemon=True
    ).start()


def _combos_worker(product_name: str, min_dur: float, max_dur: float):
    global _combos_running
    try:
        _generate_combos(product_name, min_dur, max_dur)
    except Exception as exc:
        print(f"[factory/combos] fatal: {exc}")
    finally:
        with _combos_lock:
            _combos_running = False


def _generate_combos(product_name: str, min_dur: float, max_dur: float):
    from analyzer import score_and_caption_combos

    conn = get_conn()
    ready = conn.execute("SELECT * FROM factory_modules WHERE status='ready'").fetchall()
    by_type = {"hook": [], "body": [], "cta": []}
    for m in ready:
        by_type[m["type"]].append(dict(m))

    existing = {r["name"] for r in conn.execute("SELECT name FROM factory_combos").fetchall()}
    new_combos = []
    for h, b, c in cartesian(by_type["hook"], by_type["body"], by_type["cta"]):
        name = f"{h['label']}-{b['label']}-{c['label']}"
        if name in existing:
            continue
        total = h["duration"] + b["duration"] + c["duration"]
        if not (min_dur <= total <= max_dur):
            continue
        tags = [json.loads(m["tags"] or "[]") for m in (h, b, c)]
        if not _tags_compatible(*tags):
            continue
        conn.execute(
            "INSERT INTO factory_combos (name, hook_id, body_id, cta_id, duration) VALUES (?,?,?,?,?)",
            (name, h["id"], b["id"], c["id"], round(total, 2)),
        )
        new_combos.append({"name": name, "hook": h, "body": b, "cta": c})
    conn.commit()

    # Concat por combo (rápido: -c copy)
    for combo in new_combos:
        out = OUT_DIR / f"{combo['name']}.mp4"
        try:
            concat(
                [combo["hook"]["seg_path"], combo["body"]["seg_path"], combo["cta"]["seg_path"]],
                out,
            )
            conn.execute(
                "UPDATE factory_combos SET status='ready', output_path=? WHERE name=?",
                (str(out), combo["name"]),
            )
        except Exception as exc:
            conn.execute(
                "UPDATE factory_combos SET status='error', error=? WHERE name=?",
                (str(exc)[:300], combo["name"]),
            )
        conn.commit()

    # Score + caption con Claude en tandas
    pending = [c for c in new_combos]
    for i in range(0, len(pending), 8):
        batch = pending[i:i + 8]
        payload = [{
            "name": c["name"],
            "hook": c["hook"]["transcript"][:400],
            "hook_overlay": c["hook"]["overlay_text"],
            "body": c["body"]["transcript"][:600],
            "cta": c["cta"]["transcript"][:300],
        } for c in batch]
        try:
            results = score_and_caption_combos(payload, product_name)
            for r in results:
                conn.execute(
                    "UPDATE factory_combos SET score=?, score_reason=?, caption=? WHERE name=?",
                    (r.get("score"), r.get("reason", ""), r.get("caption", ""), r.get("name")),
                )
            conn.commit()
        except Exception as exc:
            print(f"[factory/score] tanda {i // 8}: {exc}")

    conn.close()
    write_manifest()


def write_manifest() -> Path:
    conn = get_conn()
    rows = conn.execute("""
        SELECT co.name, co.duration, co.score, co.score_reason, co.caption,
               co.output_path, co.views, co.likes, co.gmv, co.published_at, co.status,
               h.label AS hook, h.overlay_text AS hook_text,
               b.label AS body, c.label AS cta
        FROM factory_combos co
        JOIN factory_modules h ON h.id = co.hook_id
        JOIN factory_modules b ON b.id = co.body_id
        JOIN factory_modules c ON c.id = co.cta_id
        ORDER BY co.score DESC NULLS LAST, co.name
    """).fetchall()
    conn.close()
    path = OUT_DIR / "manifest.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["video", "archivo", "duracion_s", "score", "razon", "caption",
                    "hook", "texto_hook", "cuerpo", "cta", "views", "likes", "gmv",
                    "publicado", "estado"])
        for r in rows:
            w.writerow([r["name"], Path(r["output_path"] or "").name, r["duration"],
                        r["score"], r["score_reason"], r["caption"], r["hook"],
                        r["hook_text"], r["body"], r["cta"], r["views"], r["likes"],
                        r["gmv"], r["published_at"], r["status"]])
    return path


# ─── Métricas (CSV semanal) y atribución por módulo ──────────────────────────

_COMBO_RE = re.compile(r"\bH\d+-B\d+-C\d+\b", re.IGNORECASE)

_METRIC_COLS = {
    "views": {"views", "vistas", "video views", "reproducciones", "vv"},
    "likes": {"likes", "me gusta"},
    "gmv": {"gmv", "ventas", "revenue", "ingresos", "gross revenue", "sales"},
}


def _parse_number(raw: str) -> float | None:
    s = str(raw or "").strip().replace("$", "").replace(",", "")
    if not s:
        return None
    mult = 1.0
    if s[-1].lower() == "k":
        mult, s = 1e3, s[:-1]
    elif s[-1].lower() == "m":
        mult, s = 1e6, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return None


def import_metrics_csv(text: str) -> dict:
    """Cruza un CSV exportado (Seller Center / TikTok Analytics) con los combos.
    El nombre del combo (H1-B2-C3) se busca en cualquier celda de la fila —
    por eso conviene dejar el nombre del archivo en el caption o título."""
    reader = csv.reader(text.splitlines())
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return {"matched": 0, "total_rows": 0}

    header = [h.strip().lower() for h in rows[0]]
    col_idx: dict[str, int] = {}
    for metric, names in _METRIC_COLS.items():
        for i, h in enumerate(header):
            if h in names:
                col_idx[metric] = i
                break

    conn = get_conn()
    matched = 0
    for row in rows[1:]:
        m = next((mm for cell in row for mm in [_COMBO_RE.search(cell)] if mm), None)
        if not m:
            continue
        name = m.group(0).upper()
        updates, values = [], []
        for metric, idx in col_idx.items():
            if idx < len(row):
                val = _parse_number(row[idx])
                if val is not None:
                    updates.append(f"{metric}=?")
                    values.append(val)
        if not updates:
            continue
        cur = conn.execute(
            f"UPDATE factory_combos SET {', '.join(updates)} WHERE name=?",
            (*values, name),
        )
        matched += cur.rowcount
    conn.commit()
    conn.close()
    write_manifest()
    return {"matched": matched, "total_rows": len(rows) - 1, "columns": list(col_idx)}


def module_attribution() -> list[dict]:
    """Promedio de views/GMV por módulo entre los combos con métricas —
    responde '¿qué hook/cuerpo/CTA gana?'."""
    conn = get_conn()
    out = []
    for mtype, fk in (("hook", "hook_id"), ("body", "body_id"), ("cta", "cta_id")):
        rows = conn.execute(f"""
            SELECT m.id, m.label, m.overlay_text, m.transcript,
                   COUNT(co.id) AS n,
                   AVG(co.views) AS avg_views,
                   AVG(co.gmv) AS avg_gmv,
                   AVG(co.score) AS avg_score
            FROM factory_modules m
            LEFT JOIN factory_combos co
                   ON co.{fk} = m.id AND (co.views IS NOT NULL OR co.gmv IS NOT NULL)
            WHERE m.type = ?
            GROUP BY m.id ORDER BY avg_views DESC NULLS LAST
        """, (mtype,)).fetchall()
        for r in rows:
            out.append({
                "type": mtype, "label": r["label"],
                "overlay_text": r["overlay_text"],
                "snippet": (r["transcript"] or "")[:90],
                "videos_with_metrics": r["n"],
                "avg_views": round(r["avg_views"], 0) if r["avg_views"] is not None else None,
                "avg_gmv": round(r["avg_gmv"], 2) if r["avg_gmv"] is not None else None,
                "avg_score": round(r["avg_score"], 1) if r["avg_score"] is not None else None,
            })
    conn.close()
    return out
