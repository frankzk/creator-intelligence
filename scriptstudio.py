"""Estudio de guiones de la Fábrica: descarga y transcribe videos ganadores
del producto (factory_research) y genera el mapa de ángulos + guiones
modulares (factory_scripts) con Claude. Workers en hilos, mismo patrón que
videofactory."""

import json
import threading

from database import get_conn

AUDIO_DIR = "temp_audio"

_research_lock = threading.Lock()
_research_running = False
_gen_lock = threading.Lock()
_gen_running = False
_gen_error = ""


def research_running() -> bool:
    with _research_lock:
        return _research_running


def generation_running() -> bool:
    with _gen_lock:
        return _gen_running


def generation_error() -> str:
    with _gen_lock:
        return _gen_error


# ─── Descarga + transcripción de videos de referencia ────────────────────────

def kick_research():
    global _research_running
    with _research_lock:
        if _research_running:
            return
        _research_running = True
    threading.Thread(target=_research_worker, daemon=True).start()


def _research_worker():
    global _research_running
    try:
        while True:
            conn = get_conn()
            row = conn.execute(
                "SELECT * FROM factory_research WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            conn.close()
            if not row:
                break
            _process_source(dict(row))
    except Exception as exc:
        print(f"[factory/research] fatal: {exc}")
    finally:
        with _research_lock:
            _research_running = False


def _set_research(rid: int, **fields):
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE factory_research SET {sets} WHERE id=?",
                 (*fields.values(), rid))
    conn.commit()
    conn.close()


def _process_source(row: dict):
    from scraper import download_video_audio
    from transcriber import transcribe_and_cleanup

    rid = row["id"]
    try:
        _set_research(rid, status="downloading")
        try:
            audio = download_video_audio(row["url"], AUDIO_DIR)
        except Exception as exc:
            msg = str(exc)
            if "NoneType" in msg or "Unable to" in msg:
                msg = "No se pudo descargar (¿link válido/público?) — pega la transcripción a mano"
            raise ValueError(msg) from exc
        _set_research(rid, status="transcribing")
        transcript = transcribe_and_cleanup(audio)
        if not transcript.strip():
            raise ValueError("Transcripción vacía (¿video sin voz?)")
        _set_research(rid, status="done", transcript=transcript, error="")
    except Exception as exc:
        _set_research(rid, status="error", error=str(exc)[:300])


# ─── Generación de mapa de ángulos + guiones ─────────────────────────────────

def kick_generation(campaign_id: int, product_name: str):
    global _gen_running, _gen_error
    with _gen_lock:
        if _gen_running:
            return
        _gen_running = True
        _gen_error = ""
    threading.Thread(target=_gen_worker, args=(campaign_id, product_name),
                     daemon=True).start()


def _gen_worker(campaign_id: int, product_name: str):
    global _gen_running, _gen_error
    try:
        _generate(campaign_id, product_name)
    except Exception as exc:
        print(f"[factory/scripts] fatal: {exc}")
        with _gen_lock:
            _gen_error = str(exc)[:300]
    finally:
        with _gen_lock:
            _gen_running = False


def _generate(campaign_id: int, product_name: str):
    from analyzer import build_module_scripts

    conn = get_conn()
    sources = [dict(r) for r in conn.execute(
        "SELECT * FROM factory_research WHERE campaign_id=? AND status='done' ORDER BY id",
        (campaign_id,),
    ).fetchall()]
    conn.close()
    if not sources:
        raise ValueError("No hay videos transcritos para esta campaña")

    result = build_module_scripts(product_name, sources)
    angle_map = result.get("angle_map", [])
    scripts = result.get("scripts", [])
    if not scripts:
        raise ValueError("Claude no devolvió guiones — reintenta")

    conn = get_conn()
    conn.execute("UPDATE factory_campaigns SET angle_map=? WHERE id=?",
                 (json.dumps(angle_map, ensure_ascii=False), campaign_id))
    # Regenerar reemplaza solo los pendientes; lo grabado/descartado se conserva
    conn.execute("DELETE FROM factory_scripts WHERE campaign_id=? AND status='pending'",
                 (campaign_id,))
    for s in scripts:
        stype = s.get("type")
        text = str(s.get("text") or "").strip()
        if stype not in ("hook", "body", "cta") or not text:
            continue
        try:
            est = round(float(s.get("est_seconds") or 0), 1)
        except (TypeError, ValueError):
            est = 0.0
        conn.execute("""
            INSERT INTO factory_scripts (campaign_id, type, angle, text, overlay_text, est_seconds)
            VALUES (?,?,?,?,?,?)
        """, (campaign_id, stype, str(s.get("angle") or "").strip(), text,
              str(s.get("overlay_text") or "").strip(), est))
    conn.commit()
    conn.close()
