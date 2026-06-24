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
import time
from itertools import product as cartesian
from pathlib import Path

from database import get_conn

FACTORY_DIR = Path("factory")
SRC_DIR = FACTORY_DIR / "src"            # originales subidos
NORM_DIR = FACTORY_DIR / "normalized"    # 1080x1920@30 normalizados
SEG_DIR = FACTORY_DIR / "segments"       # renderizados por Remotion
OUT_DIR = FACTORY_DIR / "output"         # combos finales
THUMB_DIR = FACTORY_DIR / "thumbs"       # pósters JPG (primer frame de cada módulo)
TMP_DIR = FACTORY_DIR / "tmp"            # trozos de subida en curso (chunked)
RENDER_DIR = Path("render")
RENDER_INPUTS = RENDER_DIR / "public" / "inputs"

TYPE_PREFIX = {"hook": "H", "body": "B", "cta": "C"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def ensure_dirs():
    for d in (SRC_DIR, NORM_DIR, SEG_DIR, OUT_DIR, THUMB_DIR, TMP_DIR, RENDER_INPUTS):
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


def make_thumb(seg_path: str | Path, label: str) -> str:
    """Primer frame del segmento → JPG pequeño para usar como póster en la UI.
    Best-effort: si ffmpeg falla, devuelve '' y la UI cae al póster por <video>."""
    try:
        ensure_dirs()
        out = THUMB_DIR / f"{label}.jpg"
        _run([_ffmpeg(), "-y", "-ss", "0.1", "-i", str(seg_path),
              "-frames:v", "1", "-vf", "scale=216:-2", "-q:v", "3", str(out)])
        return str(out)
    except Exception as exc:
        print(f"[factory/thumb] {label}: {exc}")
        return ""


# ─── Pipeline de módulos (normalizar → transcribir → renderizar) ─────────────

_pipeline_lock = threading.Lock()
_pipeline_running = False
_combos_lock = threading.Lock()
_combos_running = False


def pipeline_running() -> bool:
    return _pipeline_running


def combos_running() -> bool:
    return _combos_running


_combos_note = ""


def combos_note() -> str:
    with _combos_lock:
        return _combos_note


def _set_combos_note(note: str):
    global _combos_note
    with _combos_lock:
        _combos_note = note


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
            # Edición IA automática: fuera silencios, tomas repetidas y errores
            # de grabación. No-fatal: si Claude no responde, sigue sin cortar.
            if words:
                try:
                    changed, words2, dur2 = _smart_cut_file(
                        m["label"], m["type"], str(norm_path), words
                    )
                    if changed:
                        words, duration = words2, dur2
                        transcript = " ".join(w["text"] for w in words)
                        _set_module(conn, m["id"], duration=round(duration, 3))
                        print(f"[factory/{m['label']}] edición IA: quedó en {duration:.1f}s")
                except Exception as exc:
                    print(f"[factory/{m['label']}] edición IA omitida: {str(exc)[:150]}")
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
                seg = SEG_DIR / f"{m['label']}.mp4"
                _set_module(conn, m["id"], status="ready", seg_path=str(seg),
                            thumb_path=make_thumb(seg, m["label"]))
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


# ─── Miniaturas (póster JPG por módulo) ──────────────────────────────────────

def backfill_thumbs():
    """Genera el póster de módulos ya renderizados que aún no lo tienen
    (para que la librería existente gane miniatura sin re-renderizar)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT id,label,seg_path FROM factory_modules "
        "WHERE status='ready' AND IFNULL(seg_path,'')<>'' AND IFNULL(thumb_path,'')=''"
    ).fetchall()
    conn.close()
    for r in rows:
        if Path(r["seg_path"]).exists():
            t = make_thumb(r["seg_path"], r["label"])
            if t:
                c = get_conn()
                _set_module(c, r["id"], thumb_path=t)
                c.close()


def kick_thumb_backfill():
    threading.Thread(target=backfill_thumbs, daemon=True).start()


# ─── Alta de módulo (subida directa o por trozos) y limpieza de temporales ───

def create_module(conn, *, type, campaign_id, label, src_path, original_name,
                  persona="", overlay_text="", script_id="", tags="") -> int:
    """Inserta la fila del módulo (enlazando el guion si viene) y devuelve su id.
    Asume que el archivo ya está en src_path y que `label` ya fue reservado con
    next_label(). Lo usan el endpoint single-shot y el de subida por trozos."""
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    overlay = (overlay_text or "").strip()
    persona = (persona or "").strip()              # persona activa (del scope); filtra todo
    angle = ""
    sid = None
    if str(script_id or "").strip():
        srow = conn.execute("SELECT * FROM factory_scripts WHERE id=?",
                            (int(script_id),)).fetchone()
        if srow:
            sid = srow["id"]
            angle = srow["angle"] or ""
            if not overlay:
                overlay = srow["overlay_text"] or ""
            # El guion manda la persona: cada persona es un mini-proyecto.
            if srow["persona"]:
                persona = (srow["persona"] or "").strip()
            conn.execute("UPDATE factory_scripts SET status='recorded' WHERE id=?", (sid,))
    if persona and persona not in tag_list:
        tag_list.append(persona)                   # tag de respaldo para vistas antiguas
    cur = conn.execute("""
        INSERT INTO factory_modules
            (campaign_id, type, label, original_name, src_path, persona, tags, overlay_text, script_id, angle)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (campaign_id, type, label, original_name, str(src_path), persona,
          json.dumps(tag_list, ensure_ascii=False), overlay, sid, angle))
    conn.commit()
    return cur.lastrowid


def cleanup_tmp(max_age_h: float = 24):
    """Borra trozos de subidas abandonadas (best-effort, al arrancar)."""
    try:
        ensure_dirs()
        cutoff = time.time() - max_age_h * 3600
        for p in TMP_DIR.glob("*.part"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    except Exception:
        pass


# ─── Edición inteligente (silencios, tomas repetidas, duración) ──────────────

_edits_lock = threading.Lock()
_edits_active = 0
_edit_note = ""


def edits_running() -> bool:
    with _edits_lock:
        return _edits_active > 0


def edit_note() -> str:
    with _edits_lock:
        return _edit_note


def _set_edit_note(note: str):
    global _edit_note
    with _edits_lock:
        _edit_note = note


def _words_to_segments(words: list[dict]) -> list[dict]:
    """Agrupa palabras en frases: corta por pausa >0.6s o puntuación final."""
    groups, cur = [], []
    for w in words:
        if cur and (w["start"] - cur[-1]["end"] > 0.6):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w["text"][-1:] in ".!?…":
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return [{"i": i, "start": g[0]["start"], "end": g[-1]["end"],
             "text": " ".join(w["text"] for w in g)}
            for i, g in enumerate(groups) if g]


def _ranges_from_keep(segments: list[dict], keep: list[int], total_dur: float,
                      lead: float = 0.15, tail: float = 0.35,
                      join_gap: float = 0.45) -> list[list[float]]:
    """Frases conservadas → rangos de corte. Vecinas casi contiguas se unen;
    los huecos grandes entre conservadas se eliminan (fuera silencios)."""
    ranges: list[list[float]] = []
    for i in keep:
        s = segments[i]
        a = max(0.0, s["start"] - lead)
        b = min(total_dur, s["end"] + tail)
        if ranges and a - ranges[-1][1] <= join_gap:
            ranges[-1][1] = max(ranges[-1][1], b)
        else:
            ranges.append([a, b])
    return [r for r in ranges if r[1] - r[0] > 0.15]


def _enforce_target(segments: list[dict], keep: list[int], target: float) -> list[int]:
    """Garantiza que la voz conservada quepa en target: suelta frases del final."""
    def dur(ks):
        return sum(segments[i]["end"] - segments[i]["start"] for i in ks)
    keep = list(keep)
    while len(keep) > 1 and dur(keep) > target:
        keep.pop()
    return keep


def _extract_ranges(src: str | Path, ranges: list[list[float]], dst: str | Path):
    """Corta y une los rangos en una pasada de ffmpeg (re-encode preciso)."""
    parts, concat_in = [], ""
    for i, (a, b) in enumerate(ranges):
        parts.append(
            f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]"
        )
        concat_in += f"[v{i}][a{i}]"
    fc = ";".join(parts) + f";{concat_in}concat=n={len(ranges)}:v=1:a=1[v][a]"
    _run([
        _ffmpeg(), "-y", "-i", str(src), "-filter_complex", fc,
        "-map", "[v]", "-map", "[a]", "-r", "30",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        str(dst),
    ])


def _remap_words(words: list[dict], ranges: list[list[float]]) -> list[dict]:
    """Reubica los timestamps de palabras en la línea de tiempo ya cortada."""
    out, offset = [], 0.0
    for a, b in ranges:
        for w in words:
            if w["start"] >= a - 0.05 and w["end"] <= b + 0.05:
                start = max(0.0, w["start"] - a) + offset
                out.append({"text": w["text"], "start": round(start, 3),
                            "end": round(max(0.0, w["end"] - a) + offset, 3)})
        offset += b - a
    return out


def _smart_cut_file(label: str, mtype: str, norm_path: str, words: list[dict],
                    target_seconds: float | None = None):
    """Edita el archivo normalizado en sitio con cortes elegidos por Claude.
    Devuelve (changed, new_words, new_duration). Lanza ValueError si no hay voz."""
    from analyzer import select_keep_segments

    if not words:
        raise ValueError("sin transcripción palabra a palabra (¿clip sin voz?)")
    segments = _words_to_segments(words)
    if not segments:
        raise ValueError("sin frases detectadas")

    keep = select_keep_segments(segments, mtype, target_seconds)
    if target_seconds:
        keep = _enforce_target(segments, keep, target_seconds)

    total = media_duration(norm_path)
    ranges = _ranges_from_keep(segments, keep, total)
    if not ranges:
        raise ValueError("el corte quedaría vacío")
    new_dur = sum(b - a for a, b in ranges)
    # Nada que ganar: conservó todo y el ahorro es marginal
    if not target_seconds and len(keep) == len(segments) and total - new_dur < 0.8:
        return False, words, total

    cut_path = Path(norm_path).with_suffix(".cut.mp4")
    _extract_ranges(norm_path, ranges, cut_path)
    shutil.move(str(cut_path), norm_path)
    new_words = _remap_words(words, ranges)
    return True, new_words, media_duration(norm_path)


def kick_edit(module_id: int, target_seconds: float | None = None,
              regen: dict | None = None):
    """Edición manual/automática de un módulo. regen = params para regenerar
    combos al terminar (auto-recorte desde Generar combinaciones)."""
    global _edits_active
    with _edits_lock:
        _edits_active += 1
    threading.Thread(target=_edit_worker, args=(module_id, target_seconds, regen),
                     daemon=True).start()


def _edit_worker(module_id: int, target: float | None, regen: dict | None):
    global _edits_active
    label = f"módulo {module_id}"
    try:
        conn = get_conn()
        m = conn.execute("SELECT * FROM factory_modules WHERE id=?", (module_id,)).fetchone()
        if not m or not m["norm_path"]:
            conn.close()
            raise ValueError("módulo no encontrado o aún sin procesar")
        label = m["label"]
        words = json.loads(m["words"] or "[]")
        changed, new_words, new_dur = _smart_cut_file(
            m["label"], m["type"], m["norm_path"], words, target
        )
        if not changed:
            conn.close()
            _set_edit_note(f"{label}: nada que recortar (sin repeticiones ni silencios largos)")
            return
        # Los combos que usaban este módulo quedan obsoletos
        for r in conn.execute(
            "SELECT output_path FROM factory_combos WHERE hook_id=? OR body_id=? OR cta_id=?",
            (module_id, module_id, module_id),
        ).fetchall():
            if r["output_path"]:
                Path(r["output_path"]).unlink(missing_ok=True)
        conn.execute(
            "DELETE FROM factory_combos WHERE hook_id=? OR body_id=? OR cta_id=?",
            (module_id, module_id, module_id),
        )
        transcript = " ".join(w["text"] for w in new_words)
        _set_module(conn, module_id, status="transcribed", duration=round(new_dur, 3),
                    words=json.dumps(new_words, ensure_ascii=False),
                    transcript=transcript, error="")
        conn.commit()
        conn.close()
        _set_edit_note("")
        kick_pipeline()

        if regen:
            # Esperar el re-render del módulo y regenerar combos solos
            for _ in range(600):
                time.sleep(3)
                conn = get_conn()
                st = conn.execute("SELECT status FROM factory_modules WHERE id=?",
                                  (module_id,)).fetchone()
                conn.close()
                if not st or st["status"] in ("ready", "error"):
                    break
            if st and st["status"] == "ready":
                kick_combos(regen["campaign_id"], regen.get("persona", ""),
                            regen.get("product_name", ""),
                            regen["min_dur"], regen["max_dur"],
                            regen.get("mode", "focused"))
    except Exception as exc:
        print(f"[factory/edit] {label}: {exc}")
        _set_edit_note(f"{label}: {str(exc)[:200]}")
        if regen:
            _set_combos_note("El recorte automático falló — corrige el error de arriba "
                             "y vuelve a presionar Generar combinaciones.")
    finally:
        with _edits_lock:
            _edits_active -= 1


# ─── Matriz de combinaciones ──────────────────────────────────────────────────
# La compatibilidad ahora la da la persona: cada combo solo une módulos de la
# misma persona (cada persona es un mini-proyecto). Ya no se usan tags de matriz.

def _focused_triples(by_type: dict) -> list:
    """Testeo enfocado: una combinación base + variar UNA pieza a la vez.
    Da hooks+cuerpos+CTAs−2 videos (no el producto cartesiano), con señal limpia
    de qué hook / cuerpo / CTA gana. La base = primer módulo listo de cada tipo."""
    H, B, C = by_type["hook"], by_type["body"], by_type["cta"]
    if not (H and B and C):
        return []
    bh, bb, bc = H[0], B[0], C[0]
    triples = [(bh, bb, bc)]
    triples += [(h, bb, bc) for h in H[1:]]   # varía el hook
    triples += [(bh, b, bc) for b in B[1:]]   # varía el cuerpo
    triples += [(bh, bb, c) for c in C[1:]]   # varía el CTA
    return triples


def kick_combos(campaign_id: int, persona: str, product_name: str,
                min_dur: float, max_dur: float, mode: str = "focused"):
    global _combos_running
    with _combos_lock:
        if _combos_running:
            return
        _combos_running = True
    threading.Thread(
        target=_combos_worker,
        args=(campaign_id, persona, product_name, min_dur, max_dur, mode), daemon=True
    ).start()


def _combos_worker(campaign_id: int, persona: str, product_name: str,
                   min_dur: float, max_dur: float, mode: str = "focused"):
    global _combos_running
    try:
        _generate_combos(campaign_id, persona, product_name, min_dur, max_dur, mode)
    except Exception as exc:
        print(f"[factory/combos] fatal: {exc}")
    finally:
        with _combos_lock:
            _combos_running = False


def _generate_combos(campaign_id: int, persona: str, product_name: str,
                     min_dur: float, max_dur: float, mode: str = "focused"):
    from analyzer import score_and_caption_combos

    conn = get_conn()
    # Solo módulos listos de ESTA persona (cada persona es un mini-proyecto)
    ready = conn.execute(
        "SELECT * FROM factory_modules WHERE status='ready' AND campaign_id=? "
        "AND COALESCE(persona,'')=? ORDER BY id",
        (campaign_id, persona or ""),
    ).fetchall()
    by_type = {"hook": [], "body": [], "cta": []}
    for m in ready:
        by_type[m["type"]].append(dict(m))

    if mode == "all":
        triples = list(cartesian(by_type["hook"], by_type["body"], by_type["cta"]))
    else:
        triples = _focused_triples(by_type)

    existing = {r["name"] for r in conn.execute("SELECT name FROM factory_combos").fetchall()}
    new_combos = []
    totals = []
    seen = set()
    skipped_exist = skipped_dur = 0
    for h, b, c in triples:
        name = f"{h['label']}-{b['label']}-{c['label']}"
        if name in seen:
            continue
        seen.add(name)
        total = h["duration"] + b["duration"] + c["duration"]
        totals.append(total)
        if name in existing:
            skipped_exist += 1
            continue
        if not (min_dur <= total <= max_dur):
            skipped_dur += 1
            continue
        conn.execute(
            "INSERT INTO factory_combos (campaign_id, persona, name, hook_id, body_id, cta_id, duration) VALUES (?,?,?,?,?,?,?)",
            (campaign_id, persona or "", name, h["id"], b["id"], c["id"], round(total, 2)),
        )
        new_combos.append({"name": name, "hook": h, "body": b, "cta": c})
    conn.commit()

    # Aviso visible en la UI cuando no salió nada — explica el porqué
    if new_combos or not totals:
        _set_combos_note("")
    elif skipped_dur:
        # Auto-recorte: si el problema son cuerpos largos, la IA los corta sola
        # al presupuesto (max - hook más corto - CTA más corto) y regenera.
        hooks_d = [h["duration"] for h in by_type["hook"]]
        ctas_d = [c["duration"] for c in by_type["cta"]]
        budget = max_dur - min(hooks_d) - min(ctas_d) - 0.3
        target = max(4.0, budget - 1.5)  # margen por los respiros entre cortes
        long_bodies = [b for b in by_type["body"] if b["duration"] > budget]
        if budget >= 5 and long_bodies:
            for b in long_bodies:
                kick_edit(b["id"], target_seconds=target, regen={
                    "campaign_id": campaign_id, "persona": persona,
                    "product_name": product_name, "mode": mode,
                    "min_dur": min_dur, "max_dur": max_dur,
                })
            _set_combos_note(
                f"Cuerpos muy largos para videos de {max_dur:.0f}s: la IA está recortando "
                f"{len(long_bodies)} cuerpo(s) a ~{target:.0f}s (fuera silencios, tomas "
                f"repetidas y relleno). Las combinaciones se generarán solas al terminar."
            )
        else:
            _set_combos_note(
                f"0 combinaciones nuevas: el filtro es {min_dur:.0f}–{max_dur:.0f}s pero tus "
                f"combinaciones suman {min(totals):.0f}–{max(totals):.0f}s. Ajusta el rango o "
                f"revisa la duración de hooks y CTAs."
            )
    elif skipped_exist:
        _set_combos_note("Sin combinaciones nuevas: todas las posibles ya existen.")
    else:
        _set_combos_note(
            "0 combinaciones: esta persona necesita al menos 1 hook, 1 cuerpo y 1 CTA listos."
        )

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
               b.label AS body, c.label AS cta,
               ca.name AS campaign
        FROM factory_combos co
        JOIN factory_modules h ON h.id = co.hook_id
        JOIN factory_modules b ON b.id = co.body_id
        JOIN factory_modules c ON c.id = co.cta_id
        LEFT JOIN factory_campaigns ca ON ca.id = co.campaign_id
        ORDER BY ca.name, co.score DESC NULLS LAST, co.name
    """).fetchall()
    conn.close()
    path = OUT_DIR / "manifest.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["campaña", "video", "archivo", "duracion_s", "score", "razon",
                    "caption", "hook", "texto_hook", "cuerpo", "cta", "views", "likes",
                    "gmv", "publicado", "estado"])
        for r in rows:
            w.writerow([r["campaign"], r["name"], Path(r["output_path"] or "").name,
                        r["duration"], r["score"], r["score_reason"], r["caption"],
                        r["hook"], r["hook_text"], r["body"], r["cta"], r["views"],
                        r["likes"], r["gmv"], r["published_at"], r["status"]])
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
    empty = {"matched": 0, "total_rows": 0, "columns": [],
             "unmatched_codes": [], "rows_without_code": 0, "missing_metrics": []}
    if not rows:
        return empty

    header = [h.strip().lower() for h in rows[0]]
    col_idx: dict[str, int] = {}
    for metric, names in _METRIC_COLS.items():
        for i, h in enumerate(header):
            if h in names:
                col_idx[metric] = i
                break

    conn = get_conn()
    matched = 0
    rows_without_code = 0
    unmatched: list[str] = []          # códigos en el CSV que no existen como combo
    for row in rows[1:]:
        m = next((mm for cell in row for mm in [_COMBO_RE.search(cell)] if mm), None)
        if not m:
            rows_without_code += 1
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
        if cur.rowcount:
            matched += cur.rowcount
        else:
            unmatched.append(name)
    conn.commit()
    conn.close()
    write_manifest()
    return {
        "matched": matched,
        "total_rows": len(rows) - 1,
        "columns": list(col_idx),
        "unmatched_codes": list(dict.fromkeys(unmatched)),   # dedupe, conserva orden
        "rows_without_code": rows_without_code,
        "missing_metrics": [m for m in _METRIC_COLS if m not in col_idx],
    }


def module_attribution(campaign_id: int | None = None) -> list[dict]:
    """Promedio de views/GMV por módulo entre los combos con métricas —
    responde '¿qué hook/cuerpo/CTA gana?'."""
    conn = get_conn()
    out = []
    camp_filter = "AND m.campaign_id=?" if campaign_id is not None else ""
    for mtype, fk in (("hook", "hook_id"), ("body", "body_id"), ("cta", "cta_id")):
        params = (mtype, campaign_id) if campaign_id is not None else (mtype,)
        rows = conn.execute(f"""
            SELECT m.id, m.label, m.overlay_text, m.transcript, m.persona, m.angle,
                   COUNT(co.id) AS n,
                   (SELECT COUNT(*) FROM factory_combos cc WHERE cc.{fk}=m.id) AS total_combos,
                   AVG(co.views) AS avg_views,
                   AVG(co.gmv) AS avg_gmv,
                   AVG(co.score) AS avg_score
            FROM factory_modules m
            LEFT JOIN factory_combos co
                   ON co.{fk} = m.id AND (co.views IS NOT NULL OR co.gmv IS NOT NULL)
            WHERE m.type = ? {camp_filter}
            GROUP BY m.id ORDER BY avg_views DESC NULLS LAST
        """, params).fetchall()
        for r in rows:
            out.append({
                "type": mtype, "label": r["label"],
                "overlay_text": r["overlay_text"],
                "persona": r["persona"] or "",
                "angle": r["angle"] or "",
                "snippet": (r["transcript"] or "")[:90],
                "videos_with_metrics": r["n"],
                "total_combos": r["total_combos"],
                "avg_views": round(r["avg_views"], 0) if r["avg_views"] is not None else None,
                "avg_gmv": round(r["avg_gmv"], 2) if r["avg_gmv"] is not None else None,
                "avg_score": round(r["avg_score"], 1) if r["avg_score"] is not None else None,
            })
    conn.close()
    return out
