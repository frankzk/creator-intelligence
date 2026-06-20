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

_SONNET = "claude-sonnet-4-6"


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

TIKTOK_METHODOLOGY = """METODOLOGÍA DE GUION TIKTOK SHOP (aplícala):
ESTILO (todas las piezas): frases cortas de 5-8 palabras, ritmo ágil; "tú invisible"
(háblale a UNA sola persona, 1-a-1); tono conversacional y auténtico, como recomendando a
un amigo; entusiasmo natural, SIN venta agresiva ni gritos. EVITA aperturas y frases
genéricas de IA que "todos usan" — suena distinto a los demás.
HOOKS (0-3s): rompe el patrón + gancho negativo/intriga; apunta a un dolor, error común o
resultado deseado ("¿Cansada de que tu piel se vea opaca?", "Estás usando X mal y por eso Y").
NO presentes el producto todavía.
CUERPOS (varía la estructura entre ellos):
 · "Doble Caída" (efecto Zeigarnik): problema → primera solución rápida → segundo problema o
   detalle crítico que no consideraban → producto como solución definitiva.
 · Fórmula directa (estilo Flor de Cuba): problema con empatía → la solución (producto) →
   demostración y beneficios → por qué funciona.
 · Demostración / prueba social: muestra el producto en acción y resultados reales.
 En todos: "zoom en descripciones" (qué se SIENTE o qué problema específico elimina;
 BENEFICIOS, no características). Vende resolviendo problemas.
CTAs (4-8s): instrucción de compra clara y natural; menciona tocar la canasta naranja.
"""

CONTENT_PILLARS = """PILARES DE CONTENIDO (cubre VARIOS; un ángulo de hook por pilar):
1. Autoridad / prueba social — figura conocida, experto, "todos en mi FYP", reseñas reales.
2. Educación — el ingrediente / cómo funciona / por qué este y no otro.
3. Resultados / transformación — antes vs después, "a las 2 semanas noté…".
4. Demostración / unboxing / reacción — probarlo en cámara, primera vez, textura.
5. Estilo de vida / ritual — meterlo en la rutina, hábito fácil, "ya no puedo sin esto".
6. Urgencia / escasez — se agota, oferta hoy, "ojalá no lo hayas comprado ya"."""

AGGRESSIVE_SELL = """MODO VENTA AGRESIVA (máxima conversión — supervisado por el usuario):
- Pattern interrupt brutal en el primer segundo: detén el scroll sí o sí.
- Ataca el dolor en carne viva, sin rodeos; nómbralo como lo vive la persona.
- Urgencia y escasez reales (se está agotando, oferta de hoy, antes de que suba el precio).
- Contraste fuerte: el "antes" miserable contra el "después" que la persona desea.
- Órdenes directas y seguras: "deja de…", "haz esto hoy…", "corre a la canasta naranja".
- Promesas potentes pero en formato TESTIMONIO/opinión personal ("a mí me…", "en mi caso…"),
  nunca como hecho médico o garantía absoluta.
- Protege el ALCANCE de la cuenta: evita palabras que la plataforma castiga y bajan el reach
  (cura, curar, garantizado, milagro, FDA, diagnóstico, adelgaza). Vende durísimo SIN esas palabras."""


def _image_block(image_path: str) -> dict:
    """Bloque de imagen base64 para visión (jpg/png/webp)."""
    ext = Path(image_path).suffix.lower()
    media_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".png": "image/png", ".webp": "image/webp"}
    with open(image_path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode()
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_map.get(ext, "image/jpeg"), "data": b64}}


def _parse_json_loose(text: str):
    """Extrae JSON de una respuesta que puede traer prosa o citas (web search)."""
    import re
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i, j = text.find(open_c), text.rfind(close_c)
        if i != -1 and j != -1 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except Exception:
                continue
    raise ValueError("No se pudo extraer JSON de la respuesta")


def build_module_scripts(product_name: str, sources: list[dict] | None = None,
                         brief: str = "", image_path: str | None = None) -> dict:
    """Guiones modulares para UN buyer persona, a partir de la FOTO del producto
    (visión) y, opcionalmente, un texto de foco (avatar/ángulo) y transcripciones
    de videos que ya venden. El usuario trabaja una persona a la vez y acumula.
    Devuelve {"personas": [...1...], "angle_map": [...], "scripts": [...]}."""
    sources = sources or []
    blocks = []
    for i, s in enumerate(sources[:12], 1):
        note = str(s.get("notes") or "").strip()
        head = f"VIDEO {i}" + (f" ({note[:100]})" if note else "")
        blocks.append(f"{head}:\n{_safe(s.get('transcript'), 900)}")

    brief = (brief or "").strip()
    if brief:
        persona_task = (
            f"TAREA 1 — Buyer persona: trabaja UN SOLO buyer persona/ángulo definido por: "
            f"«{brief[:200]}». Si es un avatar, complétale dolor principal, deseo y objeción "
            "típica. Si es un ángulo de venta, construye el avatar más probable alrededor de "
            "ese ángulo. Devuelve exactamente 1 persona."
        )
    else:
        persona_task = (
            "TAREA 1 — Buyer persona: identifica UN (1) buyer persona, el más probable y "
            "rentable para este producto. Para él: dolor principal, deseo y objeción típica."
        )

    if blocks:
        ctx = ("Transcripciones de videos que YA están vendiendo este producto (úsalas como "
               "evidencia de qué ángulos convierten):\n\n" + "\n---\n".join(blocks) + "\n\n")
    else:
        ctx = ("No hay transcripciones de referencia: apóyate en la FOTO del producto y en tu "
               "conocimiento de qué vende en TikTok Shop.\n\n")

    prompt = (
        f"Pista del nombre/marca: {_safe(product_name, 80)}. Audiencia: hispanos en USA "
        "(TikTok Shop, video en español, voz en off sobre b-roll).\n"
        "Identifica el PRODUCTO y su beneficio principal mirando la FOTO; el nombre de arriba "
        "es solo una pista (puede ser una marca, no descriptivo).\n\n"
        + ctx
        + TIKTOK_METHODOLOGY
        + "\n\n" + AGGRESSIVE_SELL
        + "\n\n" + CONTENT_PILLARS
        + f"""

{persona_task}

TAREA 2 — Mapa de ángulos: 4-6 ángulos de venta para este persona/producto, con su evidencia
(de los videos si los hay, o tu razonamiento) y la fórmula de hook que usan.

TAREA 3 — Guiones modulares para grabar como VOZ EN OFF, para ESE ÚNICO buyer persona:
- 5 hooks (3-6s, 10-16 palabras): genera uno por cada PILAR de contenido distinto (cubre 5
  de los 6 pilares de arriba). En "angle" pon el nombre del PILAR (ej. "Urgencia / escasez").
- 3 cuerpos (15-25s, 40-65 palabras), VARÍA la estructura entre ellos: uno "Doble Caída",
  uno fórmula directa estilo Flor de Cuba, uno demostración/prueba social (ver metodología)
- 3 CTAs (4-8s, 12-22 palabras), mencionan tocar la canasta naranja

REGLAS DE COMBINABILIDAD (crítico — los módulos se combinan al azar DENTRO del persona):
- Todas las piezas del persona hablan al MISMO "tú": mismo dolor, misma promesa, mismo registro.
  Cualquier hook + cuerpo + CTA debe fluir natural al pegarse.
- Cada módulo es autocontenido: PROHIBIDO referirse a otro ("como te decía"). El cuerpo no
  saluda ni abre tema: entra directo al argumento.
- text: lenguaje hablado natural, sin emojis ni acotaciones de cámara.
- overlay_text: texto en pantalla, máx 7 palabras, estilo TikTok (puede llevar 1 emoji);
  obligatorio en hooks, opcional en cuerpos/CTAs (déjalo "" si no aporta).
- est_seconds: palabras ÷ 2.6, redondeado a 1 decimal.
- El campo "persona" de TODO guion (hooks, cuerpos y CTAs) = el nombre EXACTO de ESE único
  buyer persona. No uses "" ni mezcles personas: todo pertenece a esta persona.

Devuelve SOLO JSON:
{{
 "personas": [{{"name":"...","pain":"...","desire":"...","objection":"..."}}],
 "angle_map": [{{"angle":"...","evidence":"...","hook_formula":"plantilla con [X]"}}],
 "scripts": [
   {{"type":"hook","persona":"...","angle":"...","text":"...","overlay_text":"...","est_seconds":4.5}},
   {{"type":"body","persona":"...","angle":"...","text":"...","overlay_text":"","est_seconds":20.0}},
   {{"type":"cta","persona":"...","angle":"...","text":"...","overlay_text":"...","est_seconds":6.0}}
 ]
}}"""
    )

    # Visión: si hay foto, va como bloque de imagen
    if image_path and os.path.exists(image_path):
        user_content = [_image_block(image_path),
                        {"type": "text", "text": "Esta es la FOTO del producto.\n\n" + prompt}]
    else:
        user_content = prompt

    result = _parse_json(_call(
        messages=[{"role": "user", "content": user_content}],
        system="Eres estratega de creativos TikTok Shop para el mercado hispano de USA. "
               "Respondes SOLO con JSON válido.",
        max_tokens=8000,
    ))
    return result if isinstance(result, dict) else {}


# ─── FACTORY: DOBLAR AL GANADOR (variaciones de un hook que vendió) ──────────

def generate_hook_variations(product_name: str, persona: str, persona_detail: str,
                             winner_text: str, winner_angle: str,
                             image_path: str | None = None, n: int = 5) -> list[dict]:
    """A partir de un HOOK que ya vendió, genera N hooks nuevos que explotan el
    MISMO ángulo ganador con wording fresco. Devuelve scripts tipo hook."""
    system = ("Eres estratega de marketing de respuesta directa para TikTok Shop "
              "(hispanos en USA). Respondes SOLO con JSON válido.")
    prompt = (
        f"Producto (pista): {_safe(product_name, 80)}. Buyer persona: {_safe(persona, 120)}. "
        f"{_safe(persona_detail, 300)}\n\n"
        f"Este HOOK YA GENERÓ VENTAS (ángulo: {_safe(winner_angle, 60)}):\n"
        f"«{_safe(winner_text, 300)}»\n\n"
        + TIKTOK_METHODOLOGY + "\n\n" + AGGRESSIVE_SELL + "\n\n"
        f"Genera {n} HOOKS NUEVOS que exploten EL MISMO ángulo ganador con variaciones frescas "
        "(distinto wording y entrada, mismo gatillo psicológico que funcionó). 3-6s, 10-16 "
        "palabras, voz en off, autocontenidos, sin presentar el producto todavía.\n"
        'Devuelve SOLO JSON: {"scripts":[{"type":"hook","persona":"'
        + persona.replace('"', "'") +
        '","angle":"...","text":"...","overlay_text":"...","est_seconds":4.5}]}'
    )
    content = [prompt]
    if image_path and os.path.exists(image_path):
        content = [_image_block(image_path), {"type": "text", "text": "Foto del producto.\n\n" + prompt}]
    result = _parse_json_loose(_call(
        messages=[{"role": "user", "content": content}], system=system, max_tokens=2000))
    scripts = result.get("scripts", []) if isinstance(result, dict) else (result if isinstance(result, list) else [])
    return [s for s in scripts if isinstance(s, dict) and s.get("type") == "hook" and (s.get("text") or "").strip()]


# ─── FACTORY: INVESTIGACIÓN DE BUYER PERSONAS (web + visión) ─────────────────

def _call_web(user_content, system: str, tool_version: str, max_tokens: int = 3500) -> str:
    """Llamada con la herramienta de búsqueda web server-side. Devuelve el texto
    final (concatenado), manejando pause_turn del loop server-side."""
    client = _get_client()
    tools = [{"type": tool_version, "name": "web_search"}]
    messages = [{"role": "user", "content": user_content}]
    resp = None
    for _ in range(4):
        resp = client.messages.create(
            model=_SONNET, max_tokens=max_tokens, system=system, tools=tools, messages=messages,
        )
        if getattr(resp, "stop_reason", None) == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break
    return "\n".join(getattr(b, "text", "") for b in resp.content
                     if getattr(b, "type", None) == "text")


def _extract_personas(data) -> list[dict]:
    if isinstance(data, dict):
        data = data.get("personas", data.get("buyer_personas", []))
    return [p for p in data if isinstance(p, dict)] if isinstance(data, list) else []


def research_personas(product_name: str = "", image_path: str | None = None) -> list[dict]:
    """Descubre 2-3 buyer personas de voces reales (Reddit, foros, reseñas, YouTube)
    vía búsqueda web; si la web no rinde o no está disponible, cae al conocimiento
    del modelo. Devuelve [{name, pain, desire, objection, evidence}]."""
    system = ("Eres estratega de marketing de respuesta directa para TikTok Shop "
              "(mercado hispano de USA). Respondes SOLO con JSON válido.")
    instr = (
        f"Pista de nombre/marca: {_safe(product_name, 80)}.\n"
        "1) Identifica el PRODUCTO mirando la FOTO.\n"
        "2) INVESTIGA en la web las voces reales sobre este producto o su categoría: quejas, "
        "dolores, deseos y experiencias en Reddit, foros, reseñas, YouTube y blogs. "
        "Cita brevemente de dónde sale cada hallazgo.\n"
        "3) Destila 2-3 BUYER PERSONAS DISTINTOS. Para cada uno: name (etiqueta corta), "
        "pain (el dolor real, en sus palabras), desire, objection, evidence (qué encontraste "
        "y dónde; si te apoyaste en tu conocimiento, escribe 'razonamiento').\n"
        'Devuelve SOLO JSON: {"personas":[{"name":"...","pain":"...","desire":"...",'
        '"objection":"...","evidence":"..."}]}'
    )
    content = [instr]
    if image_path and os.path.exists(image_path):
        content = [_image_block(image_path), {"type": "text", "text": instr}]

    # 1) Búsqueda web en vivo (probando la versión nueva y la anterior de la tool)
    for tool_version in ("web_search_20260209", "web_search_20250305"):
        try:
            personas = _extract_personas(_parse_json_loose(_call_web(content, system, tool_version)))
            if personas:
                return personas
        except Exception as exc:
            print(f"[personas/web {tool_version}] {str(exc)[:160]}")

    # 2) Respaldo: conocimiento del modelo (sin web)
    raw = _call(messages=[{"role": "user", "content": content}], system=system, max_tokens=2000)
    return _extract_personas(_parse_json_loose(raw))


def name_product_from_photo(image_path: str) -> str:
    """Nombre corto y comercial del producto mirando la foto (para auto-nombrar la campaña)."""
    if not (image_path and os.path.exists(image_path)):
        return ""
    content = [_image_block(image_path), {"type": "text", "text":
        "Mira la foto del producto y devuelve SOLO un nombre corto y comercial (2 a 4 palabras, "
        "sin comillas ni punto final) para nombrar la campaña. Si hay marca visible, úsala."}]
    txt = _call(messages=[{"role": "user", "content": content}], max_tokens=30)
    name = (txt or "").strip().strip('"').splitlines()[0].strip() if txt else ""
    return name[:60]


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
