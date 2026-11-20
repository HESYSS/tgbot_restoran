"""SQLite persistence for client profiles and account-connection metadata.

Telegram session credentials deliberately stay in local session files. SQLite only
stores non-secret references, selected sender mode, and audit events.
"""
from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class ClientRegistry:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS clients (
                    chat_id INTEGER PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    sender_mode TEXT NOT NULL DEFAULT 'representative'
                        CHECK(sender_mode IN ('representative', 'personal')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dialogue_routes (
                    chat_id INTEGER PRIMARY KEY,
                    directory TEXT NOT NULL,
                    sender_mode TEXT NOT NULL,
                    waiting_answer INTEGER NOT NULL DEFAULT 0,
                    question_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS booking_forms (
                    chat_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS booking_names (
                    chat_id INTEGER PRIMARY KEY,
                    booking_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_connections (
                    chat_id INTEGER NOT NULL REFERENCES clients(chat_id) ON DELETE CASCADE,
                    connection_kind TEXT NOT NULL CHECK(connection_kind IN ('representative', 'personal')),
                    session_path TEXT NOT NULL,
                    connected_at TEXT,
                    is_active INTEGER NOT NULL DEFAULT 0 CHECK(is_active IN (0, 1)),
                    PRIMARY KEY (chat_id, connection_kind)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL REFERENCES clients(chat_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def register_client(self, chat_id: int, telegram_user_id: int, display_name: str) -> dict:
        now = self._now()
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO clients (chat_id, telegram_user_id, display_name, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET display_name=excluded.display_name, updated_at=excluded.updated_at""",
                (chat_id, telegram_user_id, display_name, now, now),
            )
        self.add_event(chat_id, "client_registered", "Client profile created or refreshed")
        return self.get_client(chat_id) or {}

    def get_client(self, chat_id: int) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM clients WHERE chat_id = ?", (chat_id,)).fetchone()
        return dict(row) if row else None

    def save_route(self, chat_id, directory, sender_mode):
        with self._connection() as connection:
            connection.execute('INSERT OR REPLACE INTO dialogue_routes VALUES (?, ?, ?, 0, NULL)',
                               (chat_id, str(Path(directory).resolve()), sender_mode))

    def route(self, chat_id):
        with self._connection() as connection:
            row = connection.execute('SELECT * FROM dialogue_routes WHERE chat_id=?', (chat_id,)).fetchone()
        return dict(row) if row else None

    def wait_for_answer(self, chat_id, question_id):
        with self._connection() as connection:
            connection.execute('UPDATE dialogue_routes SET waiting_answer=?, question_id=? WHERE chat_id=?',
                               (int(question_id is not None), question_id, chat_id))

    def remove_route(self, chat_id):
        with self._connection() as connection:
            connection.execute('DELETE FROM dialogue_routes WHERE chat_id=?', (chat_id,))

    def save_form(self, chat_id: int, form: dict) -> None:
        with self._connection() as connection:
            connection.execute("INSERT OR REPLACE INTO booking_forms VALUES (?, ?)", (chat_id, json.dumps(form)))

    def load_form(self, chat_id: int) -> dict | None:
        with self._connection() as connection:
            row = connection.execute("SELECT payload FROM booking_forms WHERE chat_id=?", (chat_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def delete_form(self, chat_id: int) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM booking_forms WHERE chat_id=?", (chat_id,))

    def save_booking_name(self, chat_id: int, name: str) -> None:
        with self._connection() as connection:
            connection.execute("INSERT OR REPLACE INTO booking_names VALUES (?, ?)", (chat_id, name))

    def booking_name(self, chat_id: int) -> str | None:
        with self._connection() as connection:
            row = connection.execute("SELECT booking_name FROM booking_names WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else None

    def set_sender_mode(self, chat_id: int, sender_mode: str) -> None:
        if sender_mode not in {"representative", "personal"}:
            raise ValueError("sender_mode must be representative or personal")
        with self._connection() as connection:
            connection.execute("UPDATE clients SET sender_mode = ?, updated_at = ? WHERE chat_id = ?", (sender_mode, self._now(), chat_id))
        self.add_event(chat_id, "sender_mode_selected", sender_mode)

    def save_connection(self, chat_id: int, connection_kind: str, session_path: Path, active: bool) -> None:
        if connection_kind not in {"representative", "personal"}:
            raise ValueError("connection_kind must be representative or personal")
        with self._connection() as connection:
            if active:
                connection.execute("UPDATE account_connections SET is_active = 0 WHERE chat_id = ?", (chat_id,))
            connection.execute(
                """INSERT INTO account_connections (chat_id, connection_kind, session_path, connected_at, is_active)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id, connection_kind) DO UPDATE SET
                       session_path=excluded.session_path, connected_at=excluded.connected_at, is_active=excluded.is_active""",
                (chat_id, connection_kind, str(session_path), self._now(), int(active)),
            )
        self.add_event(chat_id, "account_connected", connection_kind)

    def connection(self, chat_id: int, connection_kind: str) -> dict | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM account_connections WHERE chat_id = ? AND connection_kind = ?",
                (chat_id, connection_kind),
            ).fetchone()
        return dict(row) if row else None

    def remove_connection(self, chat_id: int, connection_kind: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "DELETE FROM account_connections WHERE chat_id = ? AND connection_kind = ?",
                (chat_id, connection_kind),
            )
        self.add_event(chat_id, "account_disconnected", connection_kind)

    def add_event(self, chat_id: int, event_type: str, detail: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO audit_events (chat_id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, event_type, detail, self._now()),
            )

    def recent_events(self, chat_id: int, limit: int = 10) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT event_type, detail, created_at FROM audit_events WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
