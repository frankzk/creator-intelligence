import sqlite3
import json
from pathlib import Path

DB_PATH = "database.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS creators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            display_name TEXT,
            url TEXT,
            niche TEXT DEFAULT 'General',
            video_count INTEGER DEFAULT 0,
            last_scraped TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER NOT NULL,
            tiktok_id TEXT UNIQUE NOT NULL,
            title TEXT,
            views INTEGER DEFAULT 0,
            likes INTEGER DEFAULT 0,
            comments INTEGER DEFAULT 0,
            shares INTEGER DEFAULT 0,
            duration INTEGER DEFAULT 0,
            transcript TEXT,
            hook_type TEXT,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (creator_id) REFERENCES creators(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS creator_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator_id INTEGER UNIQUE NOT NULL,
            top_hooks TEXT DEFAULT '[]',
            angles TEXT DEFAULT '[]',
            formula TEXT DEFAULT '',
            formula_steps TEXT DEFAULT '[]',
            avg_views REAL DEFAULT 0,
            top_video_count INTEGER DEFAULT 0,
            dominant_hook TEXT DEFAULT '',
            avg_duration INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (creator_id) REFERENCES creators(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            total_videos INTEGER DEFAULT 0,
            total_gmv REAL DEFAULT 0,
            top_hook TEXT DEFAULT '',
            top_angle TEXT DEFAULT '',
            ideal_duration INTEGER DEFAULT 45,
            analyzed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'pending'
        );

        CREATE TABLE IF NOT EXISTS product_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            tiktok_url TEXT,
            creator_handle TEXT,
            views INTEGER DEFAULT 0,
            gmv REAL DEFAULT 0,
            transcript TEXT,
            why_it_converts TEXT,
            scraped_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS factory_campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,         -- producto/campaña (catálogo rotativo)
            status TEXT DEFAULT 'active',      -- active | archived
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS factory_modules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            type TEXT NOT NULL,                -- hook | body | cta
            label TEXT UNIQUE NOT NULL,        -- H1, B2, C3... (únicos global, no por campaña)
            original_name TEXT,
            src_path TEXT,                     -- archivo subido
            norm_path TEXT,                    -- normalizado 1080x1920@30
            seg_path TEXT,                     -- segmento renderizado (Remotion)
            duration REAL DEFAULT 0,
            transcript TEXT DEFAULT '',
            words TEXT DEFAULT '[]',           -- timestamps por palabra (JSON)
            tags TEXT DEFAULT '[]',            -- compatibilidad de matriz (JSON)
            overlay_text TEXT DEFAULT '',      -- texto grande en pantalla
            status TEXT DEFAULT 'uploaded',    -- uploaded|normalizing|transcribing|transcribed|rendering|ready|error
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS factory_combos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER,
            name TEXT UNIQUE NOT NULL,         -- H1-B2-C3
            hook_id INTEGER NOT NULL,
            body_id INTEGER NOT NULL,
            cta_id INTEGER NOT NULL,
            output_path TEXT,
            duration REAL DEFAULT 0,
            score REAL,                        -- 0-100 coherencia (Claude)
            score_reason TEXT DEFAULT '',
            caption TEXT DEFAULT '',           -- caption + hashtags listos
            views INTEGER,
            likes INTEGER,
            gmv REAL,
            published_at TEXT,
            status TEXT DEFAULT 'pending',     -- pending|ready|error
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (hook_id) REFERENCES factory_modules(id) ON DELETE CASCADE,
            FOREIGN KEY (body_id) REFERENCES factory_modules(id) ON DELETE CASCADE,
            FOREIGN KEY (cta_id) REFERENCES factory_modules(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS factory_research (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            url TEXT DEFAULT '',               -- link TikTok del video ganador
            transcript TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',     -- pending|downloading|transcribing|done|error
            error TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS factory_scripts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL,
            type TEXT NOT NULL,                -- hook | body | cta
            persona TEXT DEFAULT '',           -- buyer persona ('' = genérico, combina con todas)
            angle TEXT DEFAULT '',             -- ángulo de venta (del mapa)
            text TEXT NOT NULL,                -- guion de voz en off
            overlay_text TEXT DEFAULT '',      -- texto en pantalla sugerido
            est_seconds REAL DEFAULT 0,
            status TEXT DEFAULT 'pending',     -- pending|recorded|discarded
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Migración para bases creadas antes de las campañas
    for table in ("factory_modules", "factory_combos"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "campaign_id" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN campaign_id INTEGER")
    # Migración para el estudio de guiones
    mod_cols = {r["name"] for r in conn.execute("PRAGMA table_info(factory_modules)").fetchall()}
    if "script_id" not in mod_cols:
        conn.execute("ALTER TABLE factory_modules ADD COLUMN script_id INTEGER")
    if "angle" not in mod_cols:
        conn.execute("ALTER TABLE factory_modules ADD COLUMN angle TEXT DEFAULT ''")
    if "trim_of" not in mod_cols:
        conn.execute("ALTER TABLE factory_modules ADD COLUMN trim_of INTEGER")
    camp_cols = {r["name"] for r in conn.execute("PRAGMA table_info(factory_campaigns)").fetchall()}
    if "angle_map" not in camp_cols:
        conn.execute("ALTER TABLE factory_campaigns ADD COLUMN angle_map TEXT DEFAULT '[]'")
    if "personas" not in camp_cols:
        conn.execute("ALTER TABLE factory_campaigns ADD COLUMN personas TEXT DEFAULT '[]'")
    if "product_image" not in camp_cols:
        conn.execute("ALTER TABLE factory_campaigns ADD COLUMN product_image TEXT DEFAULT ''")
    if "product_images" not in camp_cols:
        conn.execute("ALTER TABLE factory_campaigns ADD COLUMN product_images TEXT DEFAULT '[]'")
    if "persona_candidates" not in camp_cols:
        conn.execute("ALTER TABLE factory_campaigns ADD COLUMN persona_candidates TEXT DEFAULT '[]'")
    script_cols = {r["name"] for r in conn.execute("PRAGMA table_info(factory_scripts)").fetchall()}
    if "persona" not in script_cols:
        conn.execute("ALTER TABLE factory_scripts ADD COLUMN persona TEXT DEFAULT ''")
    conn.commit()
    conn.close()


def creator_exists(username: str) -> bool:
    conn = get_conn()
    row = conn.execute("SELECT id FROM creators WHERE username=?", (username,)).fetchone()
    conn.close()
    return row is not None


def get_creator_existing_ids(creator_id: int) -> set:
    conn = get_conn()
    rows = conn.execute("SELECT tiktok_id FROM videos WHERE creator_id=?", (creator_id,)).fetchall()
    conn.close()
    return {r["tiktok_id"] for r in rows}


def upsert_creator_analysis(creator_id: int, analysis: dict):
    conn = get_conn()
    conn.execute("""
        INSERT INTO creator_analyses
            (creator_id, top_hooks, angles, formula, formula_steps, avg_views,
             top_video_count, dominant_hook, avg_duration, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(creator_id) DO UPDATE SET
            top_hooks=excluded.top_hooks,
            angles=excluded.angles,
            formula=excluded.formula,
            formula_steps=excluded.formula_steps,
            avg_views=excluded.avg_views,
            top_video_count=excluded.top_video_count,
            dominant_hook=excluded.dominant_hook,
            avg_duration=excluded.avg_duration,
            updated_at=CURRENT_TIMESTAMP
    """, (
        creator_id,
        json.dumps(analysis.get("top_hooks", []), ensure_ascii=False),
        json.dumps(analysis.get("angles", []), ensure_ascii=False),
        analysis.get("formula", ""),
        json.dumps(analysis.get("formula_steps", []), ensure_ascii=False),
        analysis.get("avg_views", 0),
        analysis.get("top_video_count", 0),
        analysis.get("dominant_hook", ""),
        analysis.get("avg_duration", 0),
    ))
    conn.commit()
    conn.close()
