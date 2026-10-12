"""SQLite storage: library items, transcripts, searchable passages, conversations and settings."""
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("LOCAL_TTS_DB", APP_DIR / "data" / "local-tts.db"))

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- One row per file in the library. rel_path is relative to the library folder,
-- always with forward slashes, e.g. "Rust/Ownership/borrowing.mp4".
CREATE TABLE IF NOT EXISTS items (
  id          INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL CHECK (kind IN ('video', 'document')),
  title       TEXT NOT NULL,
  rel_path    TEXT NOT NULL UNIQUE,
  folder      TEXT NOT NULL DEFAULT '',
  source_url  TEXT,
  size        INTEGER,
  duration    REAL,
  status      TEXT NOT NULL DEFAULT 'queued',  -- queued, downloading, transcribing, extracting, ready, error, missing
  progress    REAL NOT NULL DEFAULT 0,
  error       TEXT,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_folder ON items(folder);

-- Timestamped transcript lines for videos.
CREATE TABLE IF NOT EXISTS segments (
  id       INTEGER PRIMARY KEY,
  item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  seq      INTEGER NOT NULL,
  start    REAL NOT NULL,
  end      REAL NOT NULL,
  text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS segments_item ON segments(item_id, seq);

-- Searchable passages (document sections, ~1 minute video windows).
CREATE TABLE IF NOT EXISTS chunks (
  id       INTEGER PRIMARY KEY,
  item_id  INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  seq      INTEGER NOT NULL,
  loc      TEXT,        -- "page 4", "12:30" ...
  start    REAL,        -- seconds, for video chunks
  text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_item ON chunks(item_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, content='chunks', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
  INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
  INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

-- Full extracted text of documents (for "read aloud" and re-chunking).
CREATE TABLE IF NOT EXISTS documents (
  item_id  INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
  text     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
  id          INTEGER PRIMARY KEY,
  title       TEXT NOT NULL DEFAULT 'New conversation',
  folder      TEXT,              -- search scope when it was created
  item_id     INTEGER REFERENCES items(id) ON DELETE SET NULL,  -- video it was about
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id               INTEGER PRIMARY KEY,
  conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
  content          TEXT NOT NULL,
  video_time       REAL,
  interrupted      INTEGER NOT NULL DEFAULT 0,
  created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conversation_id, id);
"""


def connect() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 30000")
    return c


@contextmanager
def db():
    """Short-lived connection per use (safe across threads); commits on success."""
    c = connect()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.executescript(SCHEMA)


def now() -> float:
    return time.time()


def get_setting(key: str, default=None):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    with db() as c:
        c.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def rows(cursor) -> list[dict]:
    return [dict(r) for r in cursor.fetchall()]
