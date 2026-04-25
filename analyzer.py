import json
import os
import base64
from pathlib import Path

import anthropic
import httpx

# Explicit timeouts: 10 min read so long generations never hit an idle cutoff.
_timeout = httpx.Timeout(timeout=600.0, connect=10.0, read=600.0, write=30.0, pool=10.0)

client = anthropic.Anthropic(
    api_key=os.getenv("ANTHROPIC_API_KEY", ""),
    timeout=_timeout,
    max_retries=2,
)

_SONNET = "claude-sonnet-4-20250514"


def _parse_json_response(text: str) -> dict | list:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        text = parts[1].lstrip("json").strip() if len(parts) > 1 else text
    return json.loads(text)


# ─── FLOW A: CREATOR DNA ──────────────────────────────────────────────────────

def classify_hook_type(transcript: str, title: str) -> str:
    """Heuristic hook classification to avoid an API call per video."""
    text = (title + " " + transcript[:300]).lower()
    if any(w in text for w in ["nadie te dice", "secreto", "no sabías", "dato", "sin que"]):
        return "Dato shock"
    if any(w in text for w in ["dejé", "gasté", "probé", "cambié", "cambio"]):
        return "Contraste"
    if any(w in text for w in ["mi historia", "pasé por", "yo también", "me pasó"]):
        return "Testimonio"
    if any(w in text for w in [" vs ", "mejor que", "en lugar de", "alternativa"]):
        return "Comparación"
    if any(w in text for w in ["si tienes", "si sufres", "para los que", "para ti"]):
        return "Directo"
    return "Directo"


def analyze_creator_dna(creator_username: str, videos: list[dict]) -> dict:
    """
    Analyze a creator's content DNA from transcript + metadata.
    Returns structured analysis dict.
    """
    top_videos = sorted(videos, key=lambda v: v.get("views", 0), reverse=True)

    video_blocks = []
    for i, v in enumerate(top_videos[:25], 1):
        transcript = (v.get("transcript") or "")[:600]
        video_blocks.append(
            f"Video {i} | {v.get('views', 0):,} views | {v.get('duration', 0)}s\n"
            f"Título: {v.get('title', '')}\n"
            f"Transcript: {transcript}"
        )

    videos_text = "\n---\n".join(video_blocks)

    prompt = f"""Analiza el contenido de este creador de TikTok (@{creator_username}) y extrae su ADN de contenido.

Videos analizados (ordenados por views):
{videos_text}

Devuelve SOLO un JSON con esta estructura exacta (sin texto adicional):
{{
    "top_hooks": [
        {{"text": "plantilla del hook con [X] como variable", "performance_pct": 85, "count": 5}},
        {{"text": "...", "performance_pct": 72, "count": 4}},
        {{"text": "...", "performance_pct": 60, "count": 3}},
        {{"text": "...", "performance_pct": 45, "count": 2}},
        {{"text": "...", "performance_pct": 30, "count": 2}}
    ],
    "angles": ["Resultado visible", "Testimonio propio", "Comparación rival", "Urgencia stock", "Precio accesible"],
    "formula": "Dato shock (5s) → Dolor específico (8s) → Descubrimiento (10s) → Prueba visual (12s) → CTA escasez",
    "formula_steps": [
        {{"label": "Dato shock", "color": "#7C3AED"}},
        {{"label": "Dolor", "color": "#EF4444"}},
        {{"label": "Descubrimiento", "color": "#3B82F6"}},
        {{"label": "Prueba visual", "color": "#10B981"}},
        {{"label": "CTA escasez", "color": "#F59E0B"}}
    ],
    "avg_views": 847000,
    "top_video_count": 11,
    "dominant_hook": "Dato shock",
    "avg_duration": 47,
    "niche": "Beauty / Skincare"
}}"""

    response = client.messages.create(
        model=_SONNET,
        max_tokens=2000,
        system="Eres un experto en análisis de contenido TikTok Shop. Responde SOLO con JSON válido.",
        messages=[{"role": "user", "content": prompt}],
    )

    return _parse_json_response(response.content[0].text)


# ─── FLOW B: PRODUCT ANALYSIS ────────────────────────────────────────────────

def analyze_product_video(transcript: str, views: int, gmv: float) -> str:
    """Single-video analysis: why does this video convert?"""
    prompt = (
        f"Views: {views:,} | GMV estimado: ${gmv:,.0f}\n"
        f"Transcript: {transcript[:700]}\n\n"
        "En 2-3 oraciones explica por qué convierte este video: "
        "qué hook usa, qué ángulo de venta, cómo es la estructura y qué CTA usa. "
        "Solo el análisis, sin introducción."
    )
    response = client.messages.create(
        model=_SONNET,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def analyze_product_patterns(product_videos: list[dict]) -> dict:
    """Identify cross-video patterns for a product."""
    blocks = []
    for v in product_videos[:15]:
        blocks.append(
            f"Creator: @{v.get('creator_handle', '?')} | "
            f"Views: {v.get('views', 0):,} | GMV: ${v.get('gmv', 0):,.0f}\n"
            f"Análisis: {v.get('why_it_converts', '')}"
        )

    prompt = (
        "Analiza estos videos de TikTok Shop del mismo producto:\n\n"
        + "\n---\n".join(blocks)
        + "\n\nDevuelve SOLO JSON:\n"
        '{"top_hook": "...", "top_angle": "...", "ideal_duration": 45, '
        '"patterns": ["patrón 1", "patrón 2", "patrón 3"]}'
    )

    response = client.messages.create(
        model=_SONNET,
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json_response(response.content[0].text)


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
    """Generate TikTok scripts using creator DNA and optional product data."""

    creators_ctx = ""
    for c in creators_data:
        a = c.get("analysis", {})
        creators_ctx += (
            f"\n@{c['username']} (avg {a.get('avg_views', 0):,} views, "
            f"dur. media {a.get('avg_duration', 45)}s):\n"
            f"  Fórmula: {a.get('formula', '')}\n"
            f"  Top hooks: {json.dumps(a.get('top_hooks', [])[:3], ensure_ascii=False)}\n"
            f"  Ángulos: {', '.join((a.get('angles') or [])[:4])}\n"
        )

    product_ctx = ""
    for p in product_data:
        product_ctx += (
            f"\nProducto '{p.get('keyword')}': hook={p.get('top_hook')}, "
            f"ángulo={p.get('top_angle')}, dur ideal={p.get('ideal_duration')}s\n"
        )

    if mode == "un_creador" and creators_data:
        strategy = f"Usa ÚNICAMENTE el ADN de @{creators_data[0]['username']}."
    elif mode == "multicreador":
        strategy = (
            "Fusiona: el mejor hook del creador con mayor avg_views, "
            "la estructura del más consistente, el CTA del que más convierte."
        )
    else:
        strategy = "Analiza toda la información y decide autónomamente qué tomar de cada fuente para maximizar conversión."

    scene_schema = (
        '{"label":"Hook","timing":"0-5s","script":"...","direction":["...","...","..."]}'
    )

    prompt = f"""Genera exactamente {quantity} scripts TikTok Shop para:
Producto: {product_name}
Beneficio: {benefit or 'No especificado'}
Target: EEUU, audiencia hispanohablante
Estrategia: {strategy}

ADN creadores:{creators_ctx}
Datos producto:{product_ctx}

Devuelve SOLO un array JSON con {quantity} objetos, cada uno con:
{{
  "title": "Script N — Tipo hook",
  "duration_estimate": "44s",
  "source": "@username o 'Multicreador'",
  "hook": {{
    "visual": "descripción de qué se ve en pantalla los primeros 3s",
    "textual": "frase exacta de apertura con nota de timing",
    "audio": "sugerencia de sonido/música y por qué"
  }},
  "scenes": [
    {scene_schema},
    {{"label":"Dolor","timing":"5-13s","script":"...","direction":["..."]}},
    {{"label":"Descubrimiento","timing":"13-23s","script":"...","direction":["..."]}},
    {{"label":"Prueba","timing":"23-38s","script":"...","direction":["..."]}},
    {{"label":"CTA","timing":"38-44s","script":"...","direction":["..."]}}
  ],
  "description": "texto 150-200 chars con emoji + gancho + CTA al carrito",
  "hashtags": {{
    "viral": ["#tiktokshop","#fyp","#parati"],
    "product": ["#productospecifico"],
    "niche": ["#nicho1","#nicho2"]
  }},
  "seo_hidden": ["keyword1","keyword2","keyword3"],
  "seo_audio": ["palabra1","palabra2","palabra3"]
}}

Cada script debe tener un ángulo/hook diferente. Solo el array JSON."""

    image_content = None
    if image_path and os.path.exists(image_path):
        ext = Path(image_path).suffix.lower()
        media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
        media_type = media_map.get(ext, "image/jpeg")
        with open(image_path, "rb") as f:
            image_data = base64.standard_b64encode(f.read()).decode()
        image_content = {"type": "base64", "media_type": media_type, "data": image_data}

    if image_content:
        user_content = [
            {"type": "image", "source": image_content},
            {"type": "text", "text": f"Imagen del producto: {product_name}.\n\n" + prompt},
        ]
    else:
        user_content = prompt

    response = client.messages.create(
        model=_SONNET,
        max_tokens=8000,
        system="Eres un experto en scripts virales para TikTok Shop. Responde SOLO con el array JSON solicitado.",
        messages=[{"role": "user", "content": user_content}],
    )

    return _parse_json_response(response.content[0].text)


# ─── GLOBAL INSIGHTS ─────────────────────────────────────────────────────────

def get_global_insights(all_creators: list[dict]) -> dict:
    """Cross-creator pattern analysis."""
    blocks = []
    for c in all_creators:
        a = c.get("analysis")
        if not a:
            continue
        blocks.append(
            f"@{c['username']} ({c.get('niche', 'General')}):\n"
            f"  Fórmula: {a.get('formula', '')}\n"
            f"  Top hooks: {', '.join(h.get('text', '') for h in (a.get('top_hooks') or [])[:3])}\n"
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
        "Analiza patrones comunes entre estos creadores de TikTok Shop:\n\n"
        + "\n---\n".join(blocks)
        + '\n\nDevuelve SOLO JSON:\n'
        '{"total_videos_analyzed":150,"avg_views_all":523000,'
        '"dominant_hook":"Dato shock","best_duration":"40-50s",'
        '"patterns":[{"pattern":"descripción","creators":["@c1","@c2"]}],'
        '"top_angles":["Ángulo 1","Ángulo 2","Ángulo 3"],'
        '"insights":["insight 1","insight 2","insight 3"]}'
    )

    response = client.messages.create(
        model=_SONNET,
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_json_response(response.content[0].text)
