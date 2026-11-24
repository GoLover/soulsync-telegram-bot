import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from .soulsync import Row

WEEK = 7 * 86400

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS processed_updates (
    update_id INTEGER PRIMARY KEY, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, version TEXT NOT NULL,
    message_id INTEGER, prompt TEXT, context TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actions (
    token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES sessions ON DELETE CASCADE,
    version TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_user ON actions(user_id);
CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
    data TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id INTEGER NOT NULL, playlist_id INTEGER,
    status TEXT NOT NULL, data TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_pipeline ON operations(playlist_id)
    WHERE kind = 'pipeline' AND status IN ('queued', 'starting', 'running');
CREATE TABLE IF NOT EXISTS views (
    operation_id TEXT NOT NULL REFERENCES operations ON DELETE CASCADE, user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, rendered TEXT,
    PRIMARY KEY (operation_id, user_id)
);
PRAGMA user_version = 1;
COMMIT;
"""


def token() -> str:
    return secrets.token_urlsafe(12)


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            path.touch(mode=0o600, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                conn.executescript(SCHEMA)
            elif version != 1:
                raise RuntimeError("Unsupported bot database version.")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn

    def seen(self, update_id: int) -> bool:
        with self.connection() as conn:
            return not conn.execute(
                "INSERT OR IGNORE INTO processed_updates VALUES (?, ?)", (update_id, time.time())
            ).rowcount

    def screen(
        self, user: int, chat: int, *, prompt: str | None = None, context: Row | None = None
    ) -> str:
        version = token()
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO sessions VALUES (?, ?, ?, NULL, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,
                   version=excluded.version, message_id=NULL, prompt=excluded.prompt,
                   context=excluded.context, updated_at=excluded.updated_at""",
                (user, chat, version, prompt, json.dumps(context or {}), time.time()),
            )
            conn.execute("DELETE FROM actions WHERE user_id=?", (user,))
        return version

    def bind(self, user: int, version: str, message_id: int) -> None:
        with self.connection() as conn:
            conn.execute(
                "UPDATE sessions SET message_id=? WHERE user_id=? AND version=?",
                (message_id, user, version),
            )

    def session(self, user: int, chat: int) -> Row | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE user_id=? AND chat_id=?", (user, chat)
            ).fetchone()
        return dict(row) | {"context": json.loads(row["context"])} if row else None

    def action(self, user: int, version: str, kind: str, data: Row) -> str:
        value = token()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?)",
                (value, user, version, kind, json.dumps(data), time.time() + WEEK),
            )
        return value

    def resolve(self, user: int, chat: int, message: int, value: str) -> tuple[str, Row] | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT a.kind, a.data FROM actions a JOIN sessions s USING (user_id)
                   WHERE a.token=? AND a.user_id=? AND s.chat_id=? AND s.message_id=?
                   AND s.version=a.version AND a.expires_at>?""",
                (value, user, chat, message, time.time()),
            ).fetchone()
        return (row["kind"], json.loads(row["data"])) if row else None

    def snapshot(self, user: int, kind: str, data: Row, ttl: int) -> str:
        value = token()
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                (value, user, kind, json.dumps(data), time.time() + ttl),
            )
        return value

    def load_snapshot(self, user: int, kind: str, value: str) -> Row | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT data FROM snapshots WHERE id=? AND user_id=? AND kind=? AND expires_at>?",
                (value, user, kind, time.time()),
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def arm(self, value: str, kind: str, user: int, chat: int, message: int, data: Row) -> bool:
        """An operation ID is also a durable one-use write token."""
        now = time.time()
        with self.connection() as conn:
            count = conn.execute(
                "INSERT OR IGNORE INTO operations VALUES (?, ?, ?, ?, 'queued', ?, ?, ?)",
                (value, kind, user, data.get("playlist_id"), json.dumps(data), now, now),
            ).rowcount
            if count:
                conn.execute(
                    "INSERT INTO views VALUES (?, ?, ?, ?, NULL)", (value, user, chat, message)
                )
        return bool(count)

    def operation(self, value: str) -> Row | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=?", (value,)).fetchone()
        return dict(row) | {"data": json.loads(row["data"])} if row else None

    def save(self, value: str, status: str, changes: Row) -> None:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM operations WHERE id=?", (value,)).fetchone()
            if row:
                data = json.loads(row["data"]) | changes
                conn.execute(
                    "UPDATE operations SET status=?, data=?, updated_at=? WHERE id=?",
                    (status, json.dumps(data), time.time(), value),
                )
                conn.execute("UPDATE views SET rendered=NULL WHERE operation_id=?", (value,))

    def attach(self, value: str, user: int, chat: int, message: int) -> None:
        with self.connection() as conn:
            conn.execute(
                """INSERT INTO views VALUES (?, ?, ?, ?, NULL)
                   ON CONFLICT(operation_id, user_id) DO UPDATE
                   SET chat_id=excluded.chat_id, message_id=excluded.message_id, rendered=NULL""",
                (value, user, chat, message),
            )

    def views(self, value: str) -> list[Row]:
        with self.connection() as conn:
            return [
                dict(row)
                for row in conn.execute("SELECT * FROM views WHERE operation_id=?", (value,))
            ]

    def mark_view(
        self, value: str, user: int, old_message: int, message: int, render_hash: str
    ) -> None:
        with self.connection() as conn:
            conn.execute(
                """UPDATE views SET message_id=?, rendered=?
                   WHERE operation_id=? AND user_id=? AND message_id=?""",
                (message, render_hash, value, user, old_message),
            )

    def active_pipeline(self, playlist: int) -> str | None:
        with self.connection() as conn:
            row = conn.execute(
                """SELECT id FROM operations WHERE kind='pipeline' AND playlist_id=?
                   AND status IN ('queued', 'starting', 'running')""",
                (playlist,),
            ).fetchone()
        return str(row[0]) if row else None

    def pending(self) -> list[str]:
        with self.connection() as conn:
            return [
                row[0]
                for row in conn.execute(
                    """SELECT o.id FROM operations o WHERE o.status IN ('queued', 'starting', 'running')
                   OR (o.updated_at>? AND (o.status='written' OR EXISTS (
                       SELECT 1 FROM views v WHERE v.operation_id=o.id AND v.rendered IS NULL
                   )))
                   ORDER BY CASE WHEN o.status='starting' THEN 0 ELSE 1 END, o.created_at""",
                    (time.time() - WEEK,),
                )
            ]

    def prune(self) -> None:
        now = time.time()
        with self.connection() as conn:
            conn.execute("DELETE FROM actions WHERE expires_at<?", (now,))
            conn.execute("DELETE FROM snapshots WHERE expires_at<?", (now,))
            conn.execute("DELETE FROM processed_updates WHERE created_at<?", (now - WEEK,))
            conn.execute(
                """DELETE FROM operations WHERE updated_at<?
                   AND status NOT IN ('queued', 'starting', 'running')""",
                (now - WEEK,),
            )
