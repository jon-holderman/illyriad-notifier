"SQLite persistence with atomic ingestion and durable delivery state."

import json
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet
from pwdlib import PasswordHash

from .domain import Rule

DEFAULT_SETTINGS: dict[str, Any] = {
    "api_key": "",
    "ntfy_url": "",
    "ntfy_token": "",
    "topic": "illyriad",
    "poll_minutes": 60,
    "retention_days": 90,
    "delivery_enabled": False,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1),
 data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rules (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, position INTEGER NOT NULL,
 enabled INTEGER NOT NULL, type_ids TEXT NOT NULL, category_ids TEXT NOT NULL,
 town_ids TEXT NOT NULL, action TEXT NOT NULL, topic TEXT NOT NULL,
 priority INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY, type_id TEXT NOT NULL, category_id TEXT NOT NULL,
 town_id TEXT NOT NULL,
 detail TEXT NOT NULL, occurred_at TEXT NOT NULL, imported_at TEXT NOT NULL,
 is_read INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL, reason TEXT NOT NULL,
 topic TEXT NOT NULL, priority INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt TEXT NOT NULL, type_label TEXT NOT NULL, category_label TEXT NOT NULL,
 town_label TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notifications_date ON notifications(occurred_at DESC);
CREATE INDEX IF NOT EXISTS ix_notifications_due ON notifications(status, next_attempt);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY,
 notification_id TEXT NOT NULL REFERENCES notifications(id) ON DELETE CASCADE,
 attempted_at TEXT NOT NULL, outcome TEXT NOT NULL, message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS labels (
 kind TEXT NOT NULL, source_id TEXT NOT NULL, label TEXT NOT NULL,
 PRIMARY KEY(kind, source_id)
);
"""


class Store:
    def __init__(self, directory: Path, bootstrap_password: str = "") -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = directory
        self.path = directory / "notifier.sqlite3"
        key_path = directory / "secrets.json"
        if not key_path.exists():
            if self.path.exists():
                raise RuntimeError(
                    "secrets.json is missing. Restore it with the database backup."
                )
            payload = json.dumps(
                {
                    "encryption": Fernet.generate_key().decode(),
                    "session": secrets.token_urlsafe(48),
                }
            )
            fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
        keys = json.loads(key_path.read_text())
        self.cipher = Fernet(keys["encryption"].encode())
        self.session_secret: str = keys["session"]
        self.password_hash = PasswordHash.recommended()
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES(1, ?)",
                (json.dumps(DEFAULT_SETTINGS),),
            )
            exists = db.execute(
                "SELECT value FROM meta WHERE key='admin_hash'"
            ).fetchone()
            if not exists:
                if not 12 <= len(bootstrap_password) <= 200:
                    raise RuntimeError(
                        "First startup requires NOTIFIER_ADMIN_PASSWORD "
                        "with at least 12 "
                        "characters, or run illyriad-notifier init."
                    )
                self.set_meta(
                    db, "admin_hash", self.password_hash.hash(bootstrap_password)
                )
                self.set_meta(db, "session_version", secrets.token_hex(16))
        self.path.chmod(0o600)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
        db.execute(
            (
                "INSERT INTO meta VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value"
            ),
            (key, value),
        )

    def metadata(self) -> dict[str, str]:
        with self.connection() as db:
            return {r["key"]: r["value"] for r in db.execute("SELECT * FROM meta")}

    def settings(self) -> dict[str, Any]:
        with self.connection() as db:
            result = DEFAULT_SETTINGS | json.loads(
                db.execute("SELECT data FROM settings WHERE id=1").fetchone()[0]
            )
        for field in ("api_key", "ntfy_token"):
            if result[field]:
                result[field] = self.cipher.decrypt(result[field].encode()).decode()
        return result

    def save_settings(self, settings: dict[str, Any]) -> None:
        result = settings.copy()
        for field in ("api_key", "ntfy_token"):
            if result[field]:
                result[field] = self.cipher.encrypt(result[field].encode()).decode()
        with self.connection() as db:
            db.execute("UPDATE settings SET data=? WHERE id=1", (json.dumps(result),))

    def rules(self) -> list[Rule]:
        with self.connection() as db:
            return [
                Rule(**dict(r))
                for r in db.execute("SELECT * FROM rules ORDER BY position, id")
            ]

    def save_rule(self, rule: Rule) -> None:
        data = asdict(rule)
        with self.connection() as db:
            if rule.id:
                assignments = ", ".join(f"{key}=:{key}" for key in data if key != "id")
                db.execute(f"UPDATE rules SET {assignments} WHERE id=:id", data)
            else:
                data.pop("id")
                columns = ", ".join(data)
                values = ", ".join(f":{key}" for key in data)
                db.execute(f"INSERT INTO rules ({columns}) VALUES ({values})", data)

    def delete_rule(self, rule_id: int) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM rules WHERE id=?", (rule_id,))

    def catalog(self) -> dict[str, list[dict[str, str]]]:
        result: dict[str, list[dict[str, str]]] = {}
        with self.connection() as db:
            for kind in ("town", "type", "category"):
                column = kind + "_id"
                rows = db.execute(
                    f"SELECT DISTINCT {column} AS id, "
                    f"COALESCE(l.label, NULLIF(n.{kind}_label,''), ?) AS label "
                    "FROM notifications n LEFT JOIN labels l "
                    f"ON l.kind=? AND l.source_id=n.{column} "
                    "ORDER BY label, id",
                    (kind.title(), kind),
                ).fetchall()
                unique = {r["id"]: dict(r) for r in rows}
                if (
                    kind == "town"
                    and "-1" in unique
                    and unique["-1"]["label"] == "Town"
                ):
                    unique["-1"]["label"] = "No town"
                result[kind] = list(unique.values())
        return result

    def list_events(
        self,
        *,
        page: int = 1,
        town: str = "",
        type_id: str = "",
        category: str = "",
        status: str = "",
        unread: bool = False,
        since: str = "",
        until: str = "",
        query: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("town_id", town),
            ("type_id", type_id),
            ("category_id", category),
            ("status", status),
        ):
            if value:
                clauses.append(f"{column}=?")
                values.append(value)
        if unread:
            clauses.append("is_read=0")
        if since:
            clauses.append("occurred_at >= ?")
            values.append(since)
        if until:
            clauses.append("occurred_at < ?")
            values.append(until)
        if query:
            clauses.append("instr(lower(detail), lower(?)) > 0")
            values.append(query[:200])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as db:
            total = db.execute(
                "SELECT count(*) FROM notifications" + where, values
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM notifications"
                + where
                + " ORDER BY occurred_at DESC, id DESC LIMIT 40 OFFSET ?",
                [*values, (page - 1) * 40],
            ).fetchall()
        return [dict(row) for row in rows], total

    def event(self, event_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM notifications WHERE id=?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["deliveries"] = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM deliveries WHERE notification_id=? ORDER BY id DESC",
                    (event_id,),
                )
            ]
            return result

    def stats(self) -> dict[str, int]:
        with self.connection() as db:
            result = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT status, count(*) FROM notifications GROUP BY status"
                )
            }
            result["unread"] = db.execute(
                "SELECT count(*) FROM notifications WHERE is_read=0"
            ).fetchone()[0]
            result["total"] = db.execute(
                "SELECT count(*) FROM notifications"
            ).fetchone()[0]
        return result

    def authenticate(self, password: str) -> bool:
        return self.password_hash.verify(password, self.metadata()["admin_hash"])

    def change_password(self, password: str) -> None:
        if len(password) < 12 or len(password) > 200:
            raise ValueError("Use a password between 12 and 200 characters.")
        with self.connection() as db:
            self.set_meta(db, "admin_hash", self.password_hash.hash(password))
            self.set_meta(db, "session_version", secrets.token_hex(16))
