# Creator Intelligence

Sistema de análisis de creadores TikTok Shop (FastAPI + Claude API + SQLite).
Ver `README.md` para arquitectura y flujos.

## Stack
- **Backend:** FastAPI (`main.py`), SQLite (`database.py`)
- **Scraping:** yt-dlp (`scraper.py`), Playwright/Kalodata (`kalodata.py`)
- **IA:** Anthropic Claude (`analyzer.py`), faster-whisper (`transcriber.py`)
- **Frontend:** SPA estática (`static/index.html`)

## Criterios de desarrollo (ECC)

Este proyecto adopta los criterios de calidad de código de
[ECC](https://github.com/affaan-m/ECC). Las reglas viven en `.claude/rules/ecc/`
y deben seguirse al escribir o revisar código:

- **Comunes** (`.claude/rules/ecc/common/`): estilo, code review, testing,
  performance, patrones, seguridad, git workflow.
- **Python** (`.claude/rules/ecc/python/`): PEP 8 + type hints, FastAPI,
  patrones, seguridad y testing específicos.

Cada archivo de regla declara en su frontmatter los `paths:` a los que aplica
(p. ej. `**/*.py`, `**/*_api.py`). Al tocar un archivo, sigue la regla
correspondiente.

### Puntos clave a respetar
- **Seguridad:** nunca hardcodear secretos (usar `.env` / variables de entorno);
  validar inputs; queries SQL parametrizadas; no filtrar credenciales en logs.
- **Python:** PEP 8, type annotations en todas las firmas, `ruff`/`black`/`isort`.
- **FastAPI:** routers delgados, I/O con `async def`, no clientes de larga vida
  dentro de handlers, `response_model` que nunca exponga secretos.
