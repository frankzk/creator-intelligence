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

## Fábrica de creativos (Remotion)

Sube módulos grabados (hooks, cuerpos, CTAs) y el sistema los edita y arma
**todas las combinaciones H×B×C** para publicar en TikTok Shop.

### Setup adicional

```bash
# Node 18+ y ffmpeg deben estar en el PATH
cd render && npm install
```

La primera renderización descarga Chrome Headless Shell (~90 MB) automáticamente.

### Flujo

1. Sidebar → **Fábrica de creativos** → crea la **campaña** del producto de la
   semana (catálogo rotativo: cada producto ganador tiene su propia biblioteca
   de módulos y videos; al terminar el ciclo, archívala).
2. Sube 2+ hooks, 2+ cuerpos y 2+ CTAs (mp4/mov; cualquier resolución — se
   normalizan a 1080×1920@30 con audio a -14 LUFS). A los hooks ponles su
   **texto en pantalla**; a los CTAs, el texto de oferta (queda visible todo el módulo).
3. Cada módulo pasa solo por: normalizar → transcribir (captions karaoke
   palabra a palabra con faster-whisper) → renderizar segmento con Remotion.
4. **Generar combinaciones** → producto cartesiano filtrado por duración
   (default 20–45s) y tags de compatibilidad. Cada combo es un concat de
   ffmpeg sin re-encodear: el costo de render crece con los **módulos**, no con
   las combinaciones (4 hooks + 4 cuerpos + 3 CTAs = 11 renders → 48 videos).
5. Claude puntúa la coherencia hook→cuerpo→CTA (0–100) y genera el caption
   listo (español, hispanos USA, con hashtags).
6. **Cola de hoy**: los mejores sin publicar por score — publica 4–6/día y
   márcalos. Salidas en `factory/output/` + `manifest.csv`.
7. Lunes: exporta el CSV del Seller Center / TikTok Analytics e impórtalo →
   **atribución por módulo** (qué hook/cuerpo/CTA gana en views y GMV).

### Notas de la fábrica

- Para que el cruce de métricas funcione, deja el nombre del combo
  (ej. `H2-B1-C3`) en el título o caption al publicar.
- La música agrégala desde la app de TikTok al publicar (biblioteca comercial /
  sonidos trending) — no se hornea en el video, y así el concat sigue siendo válido.
- Editar el texto de un módulo re-renderiza solo ese segmento; las
  combinaciones nuevas se rearman al instante.
- Licencia Remotion: gratis para individuos y empresas de hasta 3 personas
  (remotion.dev/license). Si creces, Company License en remotion.pro.
- `cd render && npx remotion studio` abre el editor visual de la plantilla
  (`render/src/ModuleVideo.tsx`: captions, colores, posiciones, safe zones).

## Estructura de archivos

```
creator-intelligence/
├── main.py          # FastAPI app + rutas
├── scraper.py       # yt-dlp scraper de perfiles TikTok
├── transcriber.py   # faster-whisper transcripción (+ word timestamps)
├── analyzer.py      # Claude API — análisis, generación y score de combos
├── kalodata.py      # Playwright scraper de Kalodata
├── videofactory.py  # Fábrica: normalización, pipeline, matriz, métricas
├── database.py      # SQLite setup y queries
├── render/          # Worker Remotion (Node): captions karaoke + overlays
│   ├── render.mjs   # Render por lotes (un bundle, N módulos)
│   └── src/         # Composición ModuleVideo (plantilla editable)
├── static/
│   └── index.html   # Frontend SPA
├── factory/         # (generado) src / normalized / segments / output
└── uploads/         # Fotos de productos subidas
```

## Notas técnicas

- Base de datos SQLite local: `database.db`
- Modelo de transcripción: `faster-whisper base` (CPU, int8)
- Modelo de IA: `claude-sonnet-4-20250514`
- Las operaciones largas (scraping, transcripción, análisis) corren en background threads — el frontend hace polling cada 3s hasta que terminan
- El timeout del cliente Anthropic está configurado en 10 minutos para generaciones largas
