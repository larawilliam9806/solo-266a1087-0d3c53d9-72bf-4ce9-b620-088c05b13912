"""SQLite 连接与表结构。

所有连接都使用 isolation_level=None（自动提交），事务由业务层用
BEGIN IMMEDIATE / COMMIT / ROLLBACK 显式控制，保证多步写入的原子性。
"""
import sqlite3
import time

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    sealed_at TEXT,
    publication_id INTEGER REFERENCES publications (id)
);

-- 备样表：一场内同一盲码唯一
CREATE TABLE IF NOT EXISTS samples (
    session_id INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    recipe TEXT NOT NULL,
    PRIMARY KEY (session_id, code)
);

-- 评分表：一场内同一评委对同一盲码唯一
CREATE TABLE IF NOT EXISTS scores (
    session_id INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    judge TEXT NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (session_id, code, judge)
);

CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS publication_sessions (
    publication_id INTEGER NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES sessions (id),
    PRIMARY KEY (publication_id, session_id)
);

CREATE TABLE IF NOT EXISTS published_codes (
    publication_id INTEGER NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    recipe TEXT NOT NULL,
    PRIMARY KEY (publication_id, code)
);

CREATE TABLE IF NOT EXISTS published_averages (
    publication_id INTEGER NOT NULL REFERENCES publications (id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES sessions (id),
    recipe TEXT NOT NULL,
    avg_score REAL NOT NULL,
    score_count INTEGER NOT NULL,
    PRIMARY KEY (publication_id, session_id, recipe)
);

CREATE INDEX IF NOT EXISTS idx_samples_code ON samples (code);
CREATE INDEX IF NOT EXISTS idx_scores_code ON scores (code);
-- 一个盲码最多被揭盲一次
CREATE UNIQUE INDEX IF NOT EXISTS idx_published_codes_code ON published_codes (code);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(db_path, retries=50, delay=0.1):
    """建库建表。gunicorn 多 worker 同时启动时，切换 WAL 会短暂互斥，
    因此对 database is locked/busy 做有限重试，避免启动即失败。"""
    last_error = None
    for _ in range(retries):
        conn = connect(db_path)
        try:
            # WAL：读不阻塞写、写不阻塞读，配合短写事务，
            # 让不相连场次的保存不必等揭盲汇总结束。
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            time.sleep(delay)
        finally:
            conn.close()
    raise last_error


def init_app(app):
    app.teardown_appcontext(close_db)
