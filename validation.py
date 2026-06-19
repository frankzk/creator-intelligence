"""Input validation and small pure helpers shared across the API.

Centralizes boundary validation (fail-fast) and security-sensitive checks
(path traversal, allowed file types) so routers stay thin and easy to test.
Importing this module pulls in no third-party dependencies.
"""
from __future__ import annotations

from pathlib import Path

# ─── Constants ───────────────────────────────────────────────────────────────
MAX_PROFILE_VIDEOS = 25
MIN_SCRIPT_QUANTITY = 1
MAX_SCRIPT_QUANTITY = 10
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
VALID_SCRIPT_MODES = frozenset({"un_creador", "multicreador", "mejor_de_todos"})


def extract_username_from_url(url: str) -> str:
    """Extract the bare username (no leading ``@``) from a TikTok profile URL."""
    return url.rstrip("/").split("/")[-1].lstrip("@")


def validate_tiktok_url(url: str) -> str:
    """Return a cleaned TikTok profile URL or raise ``ValueError``.

    The value comes straight from the client, so validate fail-fast at the
    boundary before any scraping work is scheduled.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValueError("La URL no puede estar vacía.")
    if not cleaned.lower().startswith(("http://", "https://")):
        raise ValueError("La URL debe empezar con http:// o https://.")
    if not extract_username_from_url(cleaned):
        raise ValueError("La URL no contiene un nombre de usuario válido.")
    return cleaned


def validate_keyword(keyword: str) -> str:
    """Return a cleaned, non-empty search keyword or raise ``ValueError``."""
    cleaned = (keyword or "").strip()
    if not cleaned:
        raise ValueError("La keyword no puede estar vacía.")
    return cleaned


def validate_script_quantity(quantity: int) -> int:
    """Return ``quantity`` if within bounds, else raise ``ValueError``."""
    if not MIN_SCRIPT_QUANTITY <= quantity <= MAX_SCRIPT_QUANTITY:
        raise ValueError(
            f"quantity debe estar entre {MIN_SCRIPT_QUANTITY} y {MAX_SCRIPT_QUANTITY}."
        )
    return quantity


def validate_script_mode(mode: str) -> str:
    """Return ``mode`` if it is a known generation mode, else raise ``ValueError``."""
    if mode not in VALID_SCRIPT_MODES:
        raise ValueError(f"mode inválido: {mode!r}.")
    return mode


def is_allowed_image(filename: str | None) -> bool:
    """Whether ``filename`` has an allowed image extension."""
    return Path(filename or "").suffix.lower() in ALLOWED_IMAGE_EXTENSIONS


def safe_upload_path(filename: str | None, uploads_dir: str = "uploads") -> Path | None:
    """Resolve a client-supplied filename inside ``uploads_dir`` safely.

    Prevents path traversal: only a bare filename (no directory components) that
    resolves to an existing file inside ``uploads_dir`` is accepted. Returns the
    resolved :class:`~pathlib.Path` on success, otherwise ``None``.
    """
    if not filename:
        return None
    # Only a bare filename is allowed — reject anything with path components.
    if Path(filename).name != filename:
        return None
    base = Path(uploads_dir).resolve()
    candidate = (base / filename).resolve()
    if base not in candidate.parents:
        return None
    if not candidate.is_file():
        return None
    return candidate
