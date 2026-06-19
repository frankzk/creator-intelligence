"""Integration tests for the SQLite data layer using a temp database."""
import pytest

import database


@pytest.fixture()
def temp_db(tmp_path, monkeypatch):
    """Point the data layer at an isolated, freshly-initialised database."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    database.init_db()
    return db_file


def _insert_creator(username: str = "creator1") -> int:
    conn = database.get_conn()
    conn.execute(
        "INSERT INTO creators (username, display_name, url, status) VALUES (?,?,?,?)",
        (username, username, f"https://tiktok.com/@{username}", "pending"),
    )
    conn.commit()
    creator_id = conn.execute(
        "SELECT id FROM creators WHERE username=?", (username,)
    ).fetchone()["id"]
    conn.close()
    return creator_id


@pytest.mark.integration
def test_init_db_creates_expected_tables(temp_db):
    # Arrange / Act
    conn = database.get_conn()
    tables = {
        r["name"]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    conn.close()
    # Assert
    assert {"creators", "videos", "creator_analyses", "products", "product_videos"} <= tables


@pytest.mark.integration
def test_creator_exists_reflects_inserts(temp_db):
    assert database.creator_exists("ghost") is False
    _insert_creator("ghost")
    assert database.creator_exists("ghost") is True


@pytest.mark.integration
def test_get_creator_existing_ids_returns_video_ids(temp_db):
    # Arrange
    creator_id = _insert_creator()
    conn = database.get_conn()
    for tiktok_id in ("v1", "v2"):
        conn.execute(
            "INSERT INTO videos (creator_id, tiktok_id) VALUES (?, ?)",
            (creator_id, tiktok_id),
        )
    conn.commit()
    conn.close()
    # Act
    ids = database.get_creator_existing_ids(creator_id)
    # Assert
    assert ids == {"v1", "v2"}


@pytest.mark.integration
def test_upsert_creator_analysis_inserts_then_updates(temp_db):
    # Arrange
    creator_id = _insert_creator()
    first = {
        "top_hooks": [{"text": "x"}],
        "angles": ["a"],
        "formula": "F1",
        "formula_steps": [{"label": "s"}],
        "avg_views": 100,
        "top_video_count": 2,
        "dominant_hook": "Dato shock",
        "avg_duration": 40,
    }

    # Act — insert
    database.upsert_creator_analysis(creator_id, first)
    conn = database.get_conn()
    row = conn.execute(
        "SELECT * FROM creator_analyses WHERE creator_id=?", (creator_id,)
    ).fetchone()
    conn.close()
    # Assert — stored and JSON-serialised
    assert row["formula"] == "F1"
    assert row["avg_views"] == 100

    # Act — update (same creator_id must not duplicate the row)
    database.upsert_creator_analysis(creator_id, {**first, "formula": "F2"})
    conn = database.get_conn()
    rows = conn.execute(
        "SELECT * FROM creator_analyses WHERE creator_id=?", (creator_id,)
    ).fetchall()
    conn.close()
    # Assert — single row, updated value
    assert len(rows) == 1
    assert rows[0]["formula"] == "F2"


@pytest.mark.integration
def test_delete_creator_cascades_to_videos(temp_db):
    # Arrange
    creator_id = _insert_creator()
    conn = database.get_conn()
    conn.execute(
        "INSERT INTO videos (creator_id, tiktok_id) VALUES (?, ?)",
        (creator_id, "v1"),
    )
    conn.commit()
    # Act — ON DELETE CASCADE should remove dependent videos
    conn.execute("DELETE FROM creators WHERE id=?", (creator_id,))
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) AS c FROM videos WHERE creator_id=?", (creator_id,)
    ).fetchone()["c"]
    conn.close()
    # Assert
    assert remaining == 0
