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
_pr_lock = threading.Lock()
_pr_running = False
_pr_error = ""


def research_running() -> bool:
    with _research_lock:
        return _research_running


def generation_running() -> bool:
    with _gen_lock:
        return _gen_running


def generation_error() -> str:
    with _gen_lock:
        return _gen_error


def personas_researching() -> bool:
    with _pr_lock:
        return _pr_running


def personas_error() -> str:
    with _pr_lock:
        return _pr_error


# ─── Investigación de buyer personas (web + visión) ──────────────────────────

def kick_persona_research(campaign_id: int, product_name: str, image_path: str):
    global _pr_running, _pr_error
    with _pr_lock:
        if _pr_running:
            return
        _pr_running = True
        _pr_error = ""
    threading.Thread(target=_pr_worker, args=(campaign_id, product_name, image_path),
                     daemon=True).start()


def _pr_worker(campaign_id: int, product_name: str, image_path: str):
    global _pr_running, _pr_error
    try:
        from analyzer import research_personas
        personas = research_personas(product_name, image_path)
        if not personas:
            raise ValueError("No se obtuvieron buyer personas — reintenta")
        conn = get_conn()
        conn.execute("UPDATE factory_campaigns SET persona_candidates=? WHERE id=?",
                     (json.dumps(personas, ensure_ascii=False), campaign_id))
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[factory/personas] fatal: {exc}")
        with _pr_lock:
            _pr_error = str(exc)[:300]
    finally:
        with _pr_lock:
            _pr_running = False


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
            # Conservar el motivo real (recortado): suele indicar la causa
            # (geo-bloqueo, video privado, o extractor de yt-dlp desactualizado).
            detail = " ".join(str(exc).replace("ERROR:", "").split())[:160]
            raise ValueError(
                f"No se pudo descargar: {detail} — prueba `pip install -U yt-dlp` "
                "y reintenta, o pega la transcripción a mano"
            ) from exc
        _set_research(rid, status="transcribing")
        transcript = transcribe_and_cleanup(audio)
        if not transcript.strip():
            raise ValueError("Transcripción vacía (¿video sin voz?)")
        _set_research(rid, status="done", transcript=transcript, error="")
    except Exception as exc:
        _set_research(rid, status="error", error=str(exc)[:300])


# ─── Generación de mapa de ángulos + guiones ─────────────────────────────────

def kick_generation(campaign_id: int, product_name: str, brief: str = "",
                    image_path: str | None = None):
    global _gen_running, _gen_error
    with _gen_lock:
        if _gen_running:
            return
        _gen_running = True
        _gen_error = ""
    threading.Thread(target=_gen_worker, args=(campaign_id, product_name, brief, image_path),
                     daemon=True).start()


def _gen_worker(campaign_id: int, product_name: str, brief: str, image_path: str | None):
    global _gen_running, _gen_error
    try:
        _generate(campaign_id, product_name, brief, image_path)
    except Exception as exc:
        print(f"[factory/scripts] fatal: {exc}")
        with _gen_lock:
            _gen_error = str(exc)[:300]
    finally:
        with _gen_lock:
            _gen_running = False


def _generate(campaign_id: int, product_name: str, brief: str, image_path: str | None):
    from analyzer import build_module_scripts

    conn = get_conn()
    sources = [dict(r) for r in conn.execute(
        "SELECT * FROM factory_research WHERE campaign_id=? AND status='done' ORDER BY id",
        (campaign_id,),
    ).fetchall()]
    conn.close()

    result = build_module_scripts(product_name, sources, brief, image_path)
    new_personas = result.get("personas", [])
    new_angles = result.get("angle_map", [])
    scripts = result.get("scripts", [])
    if not scripts:
        raise ValueError("Claude no devolvió guiones — reintenta")

    conn = get_conn()
    # Acumular personas/ángulos por campaña (1 persona a la vez): merge por nombre/ángulo
    camp = conn.execute("SELECT personas, angle_map FROM factory_campaigns WHERE id=?",
                        (campaign_id,)).fetchone()

    def _load(field):
        try:
            return json.loads((camp[field] if camp else "") or "[]")
        except Exception:
            return []

    personas = _load("personas")
    by_name = {p.get("name"): i for i, p in enumerate(personas) if isinstance(p, dict)}
    for p in new_personas:
        if not isinstance(p, dict):
            continue
        if p.get("name") in by_name:
            personas[by_name[p["name"]]] = p   # actualiza la existente
        else:
            personas.append(p)
    angle_map = _load("angle_map")
    seen = {a.get("angle") for a in angle_map if isinstance(a, dict)}
    for a in new_angles:
        if isinstance(a, dict) and a.get("angle") not in seen:
            angle_map.append(a)
            seen.add(a.get("angle"))
    conn.execute("UPDATE factory_campaigns SET angle_map=?, personas=? WHERE id=?",
                 (json.dumps(angle_map, ensure_ascii=False),
                  json.dumps(personas, ensure_ascii=False), campaign_id))

    # Reemplazar SOLO los pendientes de las personas que devuelve esta generación;
    # lo grabado/descartado y los pendientes de OTRAS personas se conservan.
    touched = {str(s.get("persona") or "").strip() for s in scripts}
    qmarks = ",".join("?" for _ in touched)
    conn.execute(
        f"DELETE FROM factory_scripts WHERE campaign_id=? AND status='pending' "
        f"AND persona IN ({qmarks})",
        (campaign_id, *touched))
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
            INSERT INTO factory_scripts
                (campaign_id, type, persona, angle, text, overlay_text, est_seconds)
            VALUES (?,?,?,?,?,?,?)
        """, (campaign_id, stype, str(s.get("persona") or "").strip(),
              str(s.get("angle") or "").strip(), text,
              str(s.get("overlay_text") or "").strip(), est))
    conn.commit()
    conn.close()
