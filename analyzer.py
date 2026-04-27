import json
import os
import base64
from pathlib import Path

import anthropic
import httpx

_timeout = httpx.Timeout(timeout=600.0, connect=10.0, read=600.0, write=30.0, pool=10.0)

client = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    timeout=_timeout,
    max_retries=2,
)

_SONNET = "claude-sonnet-4-6"


def _safe(text, max_len: int = 0) -> str:
    s = str(text or "").strip()
    if max_len:
        s = s[:max_len]
    return s or "(sin contenido)"


def _parse_json(text: str) -> dict | list:
    text = text.strip()
    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    text = text.strip()
    # Find first [ or { to skip any preamble
    for start_char in ("[", "{"):
        idx = text.find(start_char)
        if idx >= 0:
            try:
                return json.loads(text[idx:])
            except json.JSONDecodeError:
                pass
    return json.loads(text)


def _call(messages: list, system: str = "", max_tokens: int = 1000) -> str:
    kwargs: dict = dict(model=_SONNET, max_tokens=max_tokens, messages=messages)
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    return resp.content[0].text


# ─── FLOW A: CREATOR DNA ───────────────────────────────────────────────────────

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
    {"label":"Dato shock", "color":"#7C3AED"},
    {"label":"Dolor", "color":"#EF4444"},
    {"label":"Descubrimiento", "color":"#3B82F6"},
    {"label":"Prueba", "color":"#10B981"},
    {"label":"CTA", "color":"#F59E0B"}
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


# ─── FLOW B: PRODUCT ANALYSIS ───────────────────────────────────────────────────────

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


# ─── SCRIPT GENERATOR ───────────────────────────────────────────────────────────────────────────────

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
        "[\n"
        "  {\n"
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
        "  }\n"
        "]\n"
        "Cada script con ángulo diferente. Solo el array JSON."
    )

    if image_path and os.path.exists(image_path):
        ext = Path(image_path).suffix.lower()
        media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
        media_type = media_map.get(ext, "image/jpeg")
        with open(image_path, "rb") as f:
            img_b64 = base64.standard_b64encode(f.read()).decode()
        intro = f"Imagen del producto: {_safe(product_name)}."
        user_content = [
            {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
            {"type": "text", "text": intro + "\n\n" + prompt},
        ]
    else:
        user_content = prompt

    return _parse_json(_call(
        messages=[{"role": "user", "content": user_content}],
        system="Eres experto en scripts virales para TikTok Shop. Responde SOLO con el array JSON.",
        max_tokens=8000,
    ))


# ─── GLOBAL INSIGHTS ─────────────────────────────────────────────────────────────────────────────

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
