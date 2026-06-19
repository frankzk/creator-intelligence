# Creator Intelligence

Sistema de análisis de creadores TikTok Shop con generación de scripts usando IA.

## Setup

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configurar credenciales

```bash
cp .env.example .env
```

Editar `.env`:
```
ANTHROPIC_API_KEY=sk-ant-...
KALODATA_EMAIL=tu@email.com
KALODATA_PASSWORD=tupassword
```

### 3. Ejecutar

```bash
python main.py
```

Abre en http://localhost:8000

---

## Flujo A — Creador

1. Click **+ Agregar creador** → pega URL del perfil TikTok (ej. `https://www.tiktok.com/@una_flor_cubana`)
2. El sistema extrae los últimos 25 videos, transcribe el audio y analiza el ADN con Claude
3. Ve el ADN, hooks, fórmula y tabla de videos del creador
4. Click **↻** en el sidebar para actualizar incrementalmente (solo videos nuevos)

## Flujo B — Producto (Kalodata)

1. Click **+ Buscar producto** → ingresa keyword (ej. `rosemary shampoo`)
2. El sistema hace login en Kalodata, extrae los top 15 videos por GMV, transcribe y analiza
3. Ve por qué convierte cada video y los patrones del producto

## Generador de scripts

1. Selecciona un creador → tab **Generar script**
2. Sube foto del producto, ingresa nombre y beneficio
3. Elige modo (Un creador / Multicreador / Mejor de todos)
4. Selecciona creadores y/o productos como base
5. Elige cantidad (3, 5 o 10 scripts)
6. Click **Generar scripts** → cada script incluye hook visual/textual/audio, escenas con dirección de rodaje, descripción, hashtags y SEO

## Estructura de archivos

```
creator-intelligence/
├── main.py          # FastAPI app + rutas
├── scraper.py       # yt-dlp scraper de perfiles TikTok
├── transcriber.py   # faster-whisper transcripción
├── analyzer.py      # Claude API — análisis y generación
├── kalodata.py      # Playwright scraper de Kalodata
├── database.py      # SQLite setup y queries
├── static/
│   └── index.html   # Frontend SPA
└── uploads/         # Fotos de productos subidas
```

## Tests

El proyecto sigue los criterios de calidad de [ECC](https://github.com/affaan-m/ECC)
(ver `.claude/rules/ecc/`). Para ejecutar la suite:

```bash
pip install -r requirements-dev.txt
pytest                                   # toda la suite
pytest --cov=validation --cov=database --cov-report=term-missing
```

- **Unit** (`@pytest.mark.unit`): validación de inputs (`validation.py`) y
  helpers puros de análisis (`analyzer.py`).
- **Integration** (`@pytest.mark.integration`): capa de datos SQLite
  (`database.py`) sobre una base temporal aislada.

## Notas técnicas

- Base de datos SQLite local: `database.db`
- Modelo de transcripción: `faster-whisper base` (CPU, int8)
- Modelo de IA: `claude-sonnet-4-20250514`
- Las operaciones largas (scraping, transcripción, análisis) corren en background threads — el frontend hace polling cada 3s hasta que terminan
- El timeout del cliente Anthropic está configurado en 10 minutos para generaciones largas
