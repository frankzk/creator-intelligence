"""Unit tests for the pure validation helpers (no external dependencies)."""
import pytest

import validation as v


# ─── extract_username_from_url ───────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.tiktok.com/@una_flor_cubana", "una_flor_cubana"),
        ("https://www.tiktok.com/@una_flor_cubana/", "una_flor_cubana"),
        ("https://tiktok.com/una_flor_cubana", "una_flor_cubana"),
        ("@handle", "handle"),
    ],
)
def test_extract_username_strips_at_and_trailing_slash(url, expected):
    # Arrange / Act
    result = v.extract_username_from_url(url)
    # Assert
    assert result == expected


# ─── validate_tiktok_url ─────────────────────────────────────────────────────

@pytest.mark.unit
def test_validate_tiktok_url_returns_trimmed_url_when_valid():
    assert v.validate_tiktok_url("  https://www.tiktok.com/@x  ") == "https://www.tiktok.com/@x"


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "   ", None])
def test_validate_tiktok_url_rejects_empty(bad):
    with pytest.raises(ValueError):
        v.validate_tiktok_url(bad)


@pytest.mark.unit
def test_validate_tiktok_url_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        v.validate_tiktok_url("ftp://tiktok.com/@x")


@pytest.mark.unit
def test_validate_tiktok_url_rejects_missing_username():
    with pytest.raises(ValueError):
        v.validate_tiktok_url("https://@")


# ─── validate_keyword ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_validate_keyword_trims_whitespace():
    assert v.validate_keyword("  rosemary shampoo  ") == "rosemary shampoo"


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "   ", None])
def test_validate_keyword_rejects_empty(bad):
    with pytest.raises(ValueError):
        v.validate_keyword(bad)


# ─── validate_script_quantity ────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("qty", [1, 3, 10])
def test_validate_script_quantity_accepts_in_range(qty):
    assert v.validate_script_quantity(qty) == qty


@pytest.mark.unit
@pytest.mark.parametrize("qty", [0, -1, 11, 100])
def test_validate_script_quantity_rejects_out_of_range(qty):
    with pytest.raises(ValueError):
        v.validate_script_quantity(qty)


# ─── validate_script_mode ────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("mode", ["un_creador", "multicreador", "mejor_de_todos"])
def test_validate_script_mode_accepts_known_modes(mode):
    assert v.validate_script_mode(mode) == mode


@pytest.mark.unit
def test_validate_script_mode_rejects_unknown():
    with pytest.raises(ValueError):
        v.validate_script_mode("rogue_mode")


# ─── is_allowed_image ────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("name", ["a.jpg", "a.JPEG", "b.png", "c.webp"])
def test_is_allowed_image_accepts_supported_extensions(name):
    assert v.is_allowed_image(name) is True


@pytest.mark.unit
@pytest.mark.parametrize("name", ["a.gif", "a.svg", "a.exe", "noext", "", None])
def test_is_allowed_image_rejects_others(name):
    assert v.is_allowed_image(name) is False


# ─── safe_upload_path ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_safe_upload_path_returns_path_for_existing_file(tmp_path):
    # Arrange
    target = tmp_path / "photo.jpg"
    target.write_bytes(b"data")
    # Act
    result = v.safe_upload_path("photo.jpg", str(tmp_path))
    # Assert
    assert result == target.resolve()


@pytest.mark.unit
def test_safe_upload_path_returns_none_for_missing_file(tmp_path):
    assert v.safe_upload_path("ghost.jpg", str(tmp_path)) is None


@pytest.mark.unit
@pytest.mark.parametrize("evil", ["../secret.env", "../../etc/passwd", "sub/dir.jpg"])
def test_safe_upload_path_blocks_path_traversal(tmp_path, evil):
    # Even if a matching file exists outside, traversal names are rejected.
    assert v.safe_upload_path(evil, str(tmp_path)) is None


@pytest.mark.unit
def test_safe_upload_path_returns_none_for_empty(tmp_path):
    assert v.safe_upload_path(None, str(tmp_path)) is None
    assert v.safe_upload_path("", str(tmp_path)) is None
