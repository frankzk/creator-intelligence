import json
import os
import base64
from pathlib import Path

import anthropic
import httpx

# 10-min read timeout so long generations never hit an idle cutoff.
_timeout = httpx.Timeout(timeout=600.0, connect=10.0, read=600.0, write=30.0, pool=10.0)

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError(
                "Falta ANTHROPIC_API_KEY en el archivo .env de la carpeta del "
                "proyecto. Agrégala (ANTHROPIC_API_KEY=sk-ant-...) y reinicia la app."
            )
        _client = anthropic.Anthropic(api_key=key, timeout=_timeout, max_retries=2)
    return _client

_SONNET = "claude-sonnet-4-20250514"


def _safe(text, max_len: int = 0) -> str:
    """Return a non-empty string, stripping to max_len if given."""
    s = str(text or "").strip()
    if max_len:
        s = s[:max_len]
    return s or "(sin contenido)"


def _parse_json(text: str) -> dict | list:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
    return json.loads(text)


def _call(messages: list, system: str = "", max_tokens: int = 1000) -> str:
    """Single wrapper for all API calls — guarantees no empty text blocks."""
    kwargs: dict = dict(model=_SONNET, max_tokens=max_tokens, messages=messages)
    if system:
        kwargs["system"] = system
    resp = _get_client().messages.create(**kwargs)
    return resp.content[0].text


# ─── FLOW A: CREATOR DNA ──────────────────────────────────────────────────────

def classify_hook_type(transcript: str, title: str) -> str:
    text = (_safe(title) + " " + _safe(transcript, 300)).lower()
    if any(w in text for w in ["nadie te dice", "secreto", "no sabías", "dato", "sin que"]):
        return "Dato shock"
    if any(w in text for w in ["dejé", "gasté", "probé", "cambié", "cambio"]):
        return "Contraste"
    if any(w in text for w in ["mi historia", "pasé por", "yo también", "me pasó"]):
        return "Testimonio"
    if any(w in text for w in [" vs ", "mejor que", "en lugar de", "alternativa"]):
        return "Comparación"
    return "Directo"


def analyze_creator_dna(creator_username: str, videos: list[dict]) -> dict:
    top = sorted(videos, key=lambda v: v.get("views", 0), reverse=True)

    blocks = []
    for i, v in enumerate(top[:25], 1):
        blocks.append(
            f"Video {i} | {v.get('views', 0):,} views | {v.get('duration', 0)}s\n"
            f"Título: {_safe(v.get('title'), 120)}\n"
            f"Transcript: {_safe(v.get('transcript'), 500)}"
        )

    prompt = (
        f"Analiza el contenido del creador @{creator_username} de TikTok Shop.\n\n"
        + "\n---\n".join(blocks)
        + """

Devuelve SOLO un JSON con esta estructura (sin texto extra):
{
  "top_hooks": [
    {"text": "plantilla con [X]", "performance_pct": 85, "count": 5},
    {"text": "...", "performance_pct": 70, "count": 4},
    {"text": "...", "performance_pct": 55, "count": 3},
    {"text": "...", "performance_pct": 40, "count": 2},
    {"text": "...", "performance_pct": 25, "count": 1}
  ],
  "angles": ["Resultado visible", "Testimonio propio", "Comparación rival", "Urgencia stock", "Precio accesible"],
  "formula": "Dato shock (5s) → Dolor (8s) → Descubrimiento (10s) → Prueba (12s) → CTA",
  "formula_steps": [
    {"label": "Dato shock", "color": "#7C3AED"},
    {"label": "Dolor", "color": "#EF4444"},
    {"label": "Descubrimiento", "color": "#3B82F6"},
    {"label": "Prueba", "color": "#10B981"},
    {"label": "CTA", "color": "#F59E0B"}
  ],
  "avg_views": 847000,
  "top_video_count": 11,
  "dominant_hook": "Dato shock",
  "avg_duration": 47,
  "niche": "Beauty / Skincare"
}"""
    )

    return _parse_json(_call(
        messages=[{"role": "user", "content": prompt}],
        system="Eres experto en análisis de contenido TikTok Shop. Responde SOLO con JSON válido.",
        max_tokens=2000,
    ))


# ─── FLOW B: PRODUCT ANALYSIS ────────────────────────────────────────────────

def analyze_product_video(transcript: str, views: int, gmv: float) -> str:
    prompt = (
        f"Views: {views:,} | GMV: ${gmv:,.0f}\n"
        f"Transcript: {_safe(transcript, 700)}\n\n"
        "En 2-3 oraciones: qué hook usa, qué ángulo, estructura y CTA. Solo el análisis."
    )
    return _call(messages=[{"role": "user", "content": prompt}], max_tokens=300)


def analyze_product_patterns(product_videos: list[dict]) -> dict:
    blocks = [
        f"@{_safe(v.get('creator_handle'))} | {v.get('views', 0):,} views | GMV ${v.get('gmv', 0):,.0f}\n"
        f"{_safe(v.get('why_it_converts'))}"
        for v in product_videos[:15]
    ]
    prompt = (
        "Analiza estos videos TikTok Shop del mismo producto:\n\n"
        + "\n---\n".join(blocks)
        + '\n\nDevuelve SOLO JSON: {"top_hook":"...","top_angle":"...","ideal_duration":45,"patterns":["..."]}'
    )
    return _parse_json(_call(messages=[{"role": "user", "content": prompt}], max_tokens=600))


# ─── SCRIPT GENERATOR ────────────────────────────────────────────────────────

def generate_scripts(
    product_name: str,
    benefit: str,
    creators_data: list[dict],
    product_data: list[dict],
    mode: str,
    quantity: int,
    image_path: str | None = None,
) -> list[dict]:

    creators_ctx = ""
    for c in creators_data:
        a = c.get("analysis") or {}
        creators_ctx += (
            f"\n@{c['username']} (avg {a.get('avg_views', 0):,} views, {a.get('avg_duration', 45)}s):\n"
            f"  Fórmula: {_safe(a.get('formula'))}\n"
            f"  Top hooks: {json.dumps(a.get('top_hooks', [])[:3], ensure_ascii=False)}\n"
            f"  Ángulos: {', '.join((a.get('angles') or [])[:4])}\n"
        )

    product_ctx = "".join(
        f"\n'{p.get('keyword')}': hook={p.get('top_hook')}, ángulo={p.get('top_angle')}, dur={p.get('ideal_duration')}s"
        for p in product_data
    )

    if mode == "un_creador" and creators_data:
        strategy = f"Usa ÚNICAMENTE el ADN de @{creators_data[0]['username']}."
    elif mode == "multicreador":
        strategy = "Fusiona el mejor hook, la estructura más consistente y el CTA que más convierte."
    else:
        strategy = "Decide autónomamente qué tomar de cada fuente para maximizar conversión."

    prompt = (
        f"Genera exactamente {quantity} scripts TikTok Shop.\n"
        f"Producto: {_safe(product_name)}\n"
        f"Beneficio: {_safe(benefit) if benefit else 'No especificado'}\n"
        f"Target: EEUU, hispanohablante\n"
        f"Estrategia: {strategy}\n"
        f"\nADN creadores:{creators_ctx or ' (ninguno seleccionado)'}"
        f"\nProductos Kalodata:{product_ctx or ' (ninguno)'}\n\n"
        f"Devuelve SOLO un array JSON con {quantity} objetos:\n"
        '[\n'
        '  {\n'
        '    "title": "Script 1 — Dato shock",\n'
        '    "duration_estimate": "44s",\n'
        '    "source": "@username",\n'
        '    "hook": {"visual": "...", "textual": "...", "audio": "..."},\n'
        '    "scenes": [\n'
        '      {"label":"Hook","timing":"0-5s","script":"...","direction":["...","..."]},\n'
        '      {"label":"Dolor","timing":"5-13s","script":"...","direction":["..."]},\n'
        '      {"label":"Descubrimiento","timing":"13-23s","script":"...","direction":["..."]},\n'
        '      {"label":"Prueba","timing":"23-38s","script":"...","direction":["..."]},\n'
        '      {"label":"CTA","timing":"38-44s","script":"...","direction":["..."]}\n'
        '    ],\n'
        '    "description": "texto 150-200 chars emoji+gancho+CTA",\n'
        '    "hashtags": {"viral":["#tiktokshop","#fyp"],"product":["#prod"],"niche":["#nicho"]},\n'
        '    "seo_hidden": ["kw1","kw2","kw3"],\n'
        '    "seo_audio": ["palabra1","palabra2"]\n'
        '  }\n'
        ']\n'
        'Cada script con ángulo diferente. Solo el array JSON.'
    )

    # Build message content — never pass empty text blocks
    if image_path and os.path.exists(image_path):
        ext = Path(image_path).suffix.lower()
        media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
        media_type = media_map.get(ext, "image/jpeg")
        with open(image_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode()

        intro = f"Imagen del producto: {_safe(product_name)}."  # never empty
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": intro + "\n\n" + prompt},
        ]
    else:
        user_content = prompt  # plain string — SDK wraps as single text block

    return _parse_json(_call(
        messages=[{"role": "user", "content": user_content}],
        system="Eres experto en scripts virales para TikTok Shop. Responde SOLO con el array JSON.",
        max_tokens=8000,
    ))


# ─── GLOBAL INSIGHTS ─────────────────────────────────────────────────────────

def get_global_insights(all_creators: list[dict]) -> dict:
    blocks = []
    for c in all_creators:
        a = c.get("analysis")
        if not a:
            continue
        blocks.append(
            f"@{c['username']} ({c.get('niche', 'General')}):\n"
            f"  Fórmula: {_safe(a.get('formula'))}\n"
            f"  Hooks: {', '.join(_safe(h.get('text')) for h in (a.get('top_hooks') or [])[:3])}\n"
            f"  Ángulos: {', '.join((a.get('angles') or [])[:4])}\n"
            f"  Avg views: {a.get('avg_views', 0):,}"
        )

    if not blocks:
        return {
            "total_videos_analyzed": 0,
            "avg_views_all": 0,
            "dominant_hook": "—",
            "best_duration": "—",
            "patterns": [],
            "top_angles": [],
            "insights": ["Agrega creadores para ver insights globales."],
        }

    prompt = (
        "Analiza patrones comunes entre estos creadores TikTok Shop:\n\n"
        + "\n---\n".join(blocks)
        + '\n\nDevuelve SOLO JSON:\n'
        '{"total_videos_analyzed":0,"avg_views_all":0,"dominant_hook":"...","best_duration":"40-50s",'
        '"patterns":[{"pattern":"...","creators":["@c1"]}],'
        '"top_angles":["..."],"insights":["..."]}'
    )

    return _parse_json(_call(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1200,
    ))


# ─── FACTORY: ESTUDIO DE GUIONES ─────────────────────────────────────────────

def build_module_scripts(product_name: str, sources: list[dict]) -> dict:
    """Mapa de ángulos + guiones modulares a partir de transcripciones de videos
    que YA venden el producto. sources: [{transcript, notes}].
    Devuelve {"angle_map": [...], "scripts": [...]} con 6 hooks, 4 cuerpos, 3 CTAs."""
    blocks = []
    for i, s in enumerate(sources[:12], 1):
        note = str(s.get("notes") or "").strip()
        head = f"VIDEO {i}" + (f" ({note[:100]})" if note else "")
        blocks.append(f"{head}:\n{_safe(s.get('transcript'), 900)}")

    prompt = (
        f"Producto: {_safe(product_name, 80)}. Audiencia: hispanos en USA "
        "(TikTok Shop, video en español, voz en off sobre b-roll).\n\n"
        "Transcripciones de videos que YA están vendiendo este producto:\n\n"
        + "\n---\n".join(blocks)
        + """

TAREA 1 — Mapa de ángulos: identifica los 4-6 ángulos de venta que estos videos
explotan (ej. miedo al problema, resultado visible, precio/oferta, testimonio,
comparación, curiosidad científica). Para cada uno: qué evidencia hay en los
videos y la fórmula de hook que usan.

TAREA 2 — Guiones modulares para grabar como VOZ EN OFF. Exactamente:
- 6 hooks (3-6s, 10-16 palabras), cada uno con un ángulo/fórmula DIFERENTE
- 4 cuerpos (15-25s, 40-65 palabras), desarrollan el argumento con beneficios/prueba
- 3 CTAs (4-8s, 12-22 palabras), siempre mencionan tocar la canasta naranja

REGLAS DE MODULARIDAD (crítico — los módulos se combinan al azar):
- Cada módulo es autocontenido: PROHIBIDO referirse a otro módulo ("como te decía",
  "además de lo anterior"). El cuerpo no saluda ni abre tema: entra directo al argumento.
- Cualquier hook debe poder pegarse con cualquier cuerpo y cualquier CTA sin sonar raro.
- text: lenguaje hablado natural, sin emojis ni acotaciones de cámara.
- overlay_text: texto en pantalla, máx 7 palabras, estilo TikTok (puede llevar 1 emoji);
  obligatorio en hooks, opcional en cuerpos/CTAs (déjalo "" si no aporta).
- est_seconds: palabras ÷ 2.6, redondeado a 1 decimal.

Devuelve SOLO JSON:
{
 "angle_map": [{"angle":"...","evidence":"...","hook_formula":"plantilla con [X]"}],
 "scripts": [
   {"type":"hook","angle":"...","text":"...","overlay_text":"...","est_seconds":4.5},
   {"type":"body","angle":"...","text":"...","overlay_text":"","est_seconds":20.0},
   {"type":"cta","angle":"...","text":"...","overlay_text":"...","est_seconds":6.0}
 ]
}"""
    )

    result = _parse_json(_call(
        messages=[{"role": "user", "content": prompt}],
        system="Eres estratega de creativos TikTok Shop para el mercado hispano de USA. "
               "Respondes SOLO con JSON válido.",
        max_tokens=6000,
    ))
    return result if isinstance(result, dict) else {}


# ─── FACTORY: EDICIÓN INTELIGENTE DE MÓDULOS ─────────────────────────────────

def select_keep_segments(segments: list[dict], module_type: str,
                         target_seconds: float | None = None) -> list[int]:
    """Decide qué frases de un módulo conservar. Elimina tomas repetidas, falsos
    inicios y rodeos (los silencios se eliminan solos al cortar por frase).
    segments: [{"i", "start", "end", "text"}]. Devuelve índices en orden."""
    lines = []
    prev_end = None
    for s in segments:
        gap = f" [pausa {s['start'] - prev_end:.1f}s antes]" if prev_end is not None and s["start"] - prev_end > 0.8 else ""
        lines.append(f"{s['i']} | {s['start']:.1f}-{s['end']:.1f}s | {s['text']}{gap}")
        prev_end = s["end"]
    total = segments[-1]["end"] - segments[0]["start"] if segments else 0
    tipo = {"hook": "HOOK (gancho inicial)", "body": "CUERPO (argumento central)",
            "cta": "CTA (cierre con llamado a la acción)"}.get(module_type, module_type)
    dur_rule = (
        f"- DURACIÓN: el resultado debe quedar en MÁXIMO {target_seconds:.0f}s de voz "
        f"(ideal {target_seconds * 0.75:.0f}–{target_seconds:.0f}s). Prioriza entrada directa "
        "al argumento, beneficio principal y prueba; corta lo demás.\n"
        if target_seconds else
        "- NO recortes contenido por duración: solo limpia repeticiones y errores.\n"
    )
    prompt = (
        f"Material crudo de un módulo {tipo} para TikTok Shop (voz en off, español). "
        f"Dura {total:.0f}s, dividido en frases con tiempos:\n\n"
        + "\n".join(lines)
        + "\n\nElige las frases a CONSERVAR para el corte final. Reglas:\n"
        "- TOMAS REPETIDAS: si una frase (o casi idéntica) aparece varias veces, conserva "
        "SOLO la mejor versión (normalmente la más completa o la última) y descarta las demás.\n"
        "- Corta falsos inicios, muletillas, frases cortadas a la mitad y errores de grabación.\n"
        + dur_rule +
        "- Mantén el orden original y que el resultado fluya natural al reproducirse seguido.\n"
        "- El cierre/despedida no hace falta aquí: lo pone otro módulo.\n\n"
        'Devuelve SOLO JSON: {"keep": [0, 2, 5]}'
    )
    result = _parse_json(_call(
        messages=[{"role": "user", "content": prompt}],
        system="Eres editor de video para TikTok Shop. Respondes SOLO con JSON válido.",
        max_tokens=400,
    ))
    keep = result.get("keep", []) if isinstance(result, dict) else []
    valid = {s["i"] for s in segments}
    keep = sorted({int(i) for i in keep if int(i) in valid})
    if not keep:
        raise ValueError("Claude no eligió frases para conservar")
    return keep


# ─── FACTORY: SCORE + CAPTIONS DE COMBOS ─────────────────────────────────────

def score_and_caption_combos(combos: list[dict], product_name: str = "") -> list[dict]:
    """Puntúa la coherencia hook→cuerpo→CTA de cada combo (0-100) y genera el
    caption listo para publicar. combos: [{name, hook, hook_overlay, body, cta}].
    Mercado: hispanos en USA — caption en español, hashtags mezclados es/en."""
    product = _safe(product_name, 80)
    blocks = []
    for c in combos:
        blocks.append(
            f"VIDEO {c['name']}:\n"
            f"  HOOK (texto en pantalla: \"{_safe(c.get('hook_overlay'), 100)}\"): {_safe(c.get('hook'), 400)}\n"
            f"  CUERPO: {_safe(c.get('body'), 600)}\n"
            f"  CTA: {_safe(c.get('cta'), 300)}"
        )

    prompt = (
        f"Eres estratega de creativos para TikTok Shop (audiencia: hispanos en USA). "
        f"Producto: {product or 'no especificado'}.\n\n"
        "Estos videos se armaron combinando módulos grabados por separado "
        "(hook + cuerpo + CTA). Evalúa cada combinación:\n\n"
        + "\n\n".join(blocks)
        + "\n\nPara cada video devuelve:\n"
        "- score 0-100: ¿fluye natural la transición hook→cuerpo→CTA? ¿la promesa "
        "del hook la cumple el cuerpo? ¿el CTA es consistente con el argumento? "
        "Castiga repeticiones de frases entre módulos y saltos de tema.\n"
        "- reason: 1 frase concreta (es).\n"
        "- caption: caption listo para publicar en español (hispanos USA), 1-2 líneas "
        "con gancho + 5 hashtags (mezcla español/inglés, incluye #tiktokshop), "
        "menciona tocar la canasta naranja. Sin comillas ni emojis excesivos.\n\n"
        'Devuelve SOLO JSON: [{"name":"H1-B1-C1","score":85,"reason":"...","caption":"..."}]'
    )

    result = _parse_json(_call(
        messages=[{"role": "user", "content": prompt}],
        max_tokens=300 * len(combos) + 200,
    ))
    return result if isinstance(result, list) else []
