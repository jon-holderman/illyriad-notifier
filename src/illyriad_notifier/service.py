"""Polling, ingestion, delivery retries and retention."""

import threading
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .domain import (
    Feed,
    Rule,
    ValidationError,
    match_rule,
    topic,
    validate_key,
    validate_server,
)
from .feed import fetch_feed
from .store import Store


class Service:
    def __init__(self, store: Store, client: httpx.Client) -> None:
        self.store = store
        self.client = client
        self.lock = threading.RLock()

    def save_settings(self, changes: dict[str, Any]) -> None:
        with self.lock:
            current = self.store.settings()
            for secret in ("api_key", "ntfy_token"):
                if not changes.get(secret):
                    changes[secret] = current[secret]
            result = current | changes
            result["api_key"] = validate_key(result["api_key"])
            result["ntfy_url"] = validate_server(result["ntfy_url"])
            result["topic"] = topic(result["topic"])
            if not 60 <= result["poll_minutes"] <= 1440:
                raise ValidationError("Polling must be between 60 and 1440 minutes.")
            if not 1 <= result["retention_days"] <= 3650:
                raise ValidationError("Retention must be between 1 and 3650 days.")
            if (
                len(result["ntfy_token"]) > 2000
                or not result["ntfy_token"].isascii()
                or any(not c.isprintable() or c.isspace() for c in result["ntfy_token"])
            ):
                raise ValidationError("Use an ASCII ntfy token without whitespace.")
            if result["delivery_enabled"] and (
                not result["ntfy_url"] or not result["topic"]
            ):
                raise ValidationError(
                    "Set an ntfy server and topic before enabling delivery."
                )
            if changes.get("clear_ntfy_token"):
                result["ntfy_token"] = ""
            result.pop("clear_ntfy_token", None)
            self.store.save_settings(result)

    def ingest(self, feed: Feed, now: datetime) -> int:
        "Commit events, delivery decisions and tombstones in one transaction."
        with self.lock, self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            meta = {r[0]: r[1] for r in db.execute("SELECT key,value FROM meta")}
            if meta.get("player_id") and meta["player_id"] != feed.player_id:
                raise ValidationError(
                    "This installation belongs to a different Illyriad "
                    "player. Use a "
                    "separate data volume for another account."
                )
            initial = "initialized" not in meta
            settings = self.store.settings()
            rules = [
                Rule(**dict(r))
                for r in db.execute("SELECT * FROM rules ORDER BY position,id")
            ]
            imported = 0
            for event in sorted(feed.events, key=lambda n: (n.occurred_at, n.id)):
                if not db.execute(
                    "INSERT OR IGNORE INTO seen VALUES(?)", (event.id,)
                ).rowcount:
                    continue
                rule = match_rule(event, rules)
                status = (
                    "history"
                    if initial
                    else "pending"
                    if rule and rule.action == "forward"
                    else "kept"
                )
                reason = (
                    "Initial history import; forwarding suppressed"
                    if initial
                    else f"Rule: {rule.name}"
                    if rule
                    else "No rule matched; kept in inbox"
                )
                values = asdict(event) | {
                    "imported_at": now.isoformat(),
                    "status": status,
                    "reason": reason,
                    "topic": (rule.topic if rule else "") or settings["topic"],
                    "priority": rule.priority if rule else 3,
                    "next_attempt": now.isoformat(),
                }
                db.execute(
                    f"INSERT INTO notifications ({','.join(values)}) "
                    f"VALUES ({','.join(':' + k for k in values)})",
                    values,
                )
                imported += 1
            for key, value in {
                "player_id": feed.player_id,
                "initialized": "1",
                "last_success": now.isoformat(),
                "last_error": "",
                "last_import_count": str(imported),
            }.items():
                self.store.set_meta(db, key, value)
            return imported

    def poll(self, now: datetime) -> str:
        with self.lock:
            settings = self.store.settings()
            if not settings["api_key"]:
                return "Save your Illyriad notification key in Settings to begin."
            meta = self.store.metadata()
            if meta.get("next_poll") and now < datetime.fromisoformat(
                meta["next_poll"]
            ):
                return (
                    "Next eligible fetch: "
                    + meta["next_poll"]
                    + ". Recent status is shown below."
                )
            with self.store.connection() as db:
                self.store.set_meta(
                    db,
                    "next_poll",
                    (now + timedelta(minutes=settings["poll_minutes"])).isoformat(),
                )
                self.store.set_meta(db, "last_attempt", now.isoformat())
            try:
                count = self.ingest(fetch_feed(self.client, settings["api_key"]), now)
                return f"Feed connected. Imported {count} new notifications."
            except ValidationError as exc:
                with self.store.connection() as db:
                    self.store.set_meta(db, "last_error", str(exc))
                return str(exc)

    def publish(
        self,
        settings: dict[str, Any],
        destination: str,
        title: str,
        body: str,
        priority: int,
    ) -> None:
        if not settings["ntfy_url"] or not destination:
            raise ValidationError("Configure an ntfy server and topic in Settings.")
        headers = (
            {"Authorization": "Bearer " + settings["ntfy_token"]}
            if settings["ntfy_token"]
            else {}
        )
        try:
            # JSON avoids non-ASCII titles becoming invalid HTTP headers.
            response = self.client.post(
                settings["ntfy_url"] + "/",
                headers=headers,
                json={
                    "topic": destination,
                    "title": title[:150],
                    "message": body.encode("utf-8")[:3500].decode(
                        "utf-8", errors="ignore"
                    )
                    or "Illyriad notification",
                    "priority": priority,
                    "tags": ["illyriad"],
                },
                follow_redirects=False,
            )
            if not 200 <= response.status_code < 300:
                raise ValidationError(f"ntfy returned HTTP {response.status_code}.")
        except httpx.HTTPError:
            raise ValidationError(
                "Could not reach ntfy. Check its URL, network and token."
            ) from None

    def send_test(self) -> None:
        with self.lock:
            settings = self.store.settings()
            self.publish(
                settings,
                settings["topic"],
                "Illyriad Notifier connected",
                (
                    "Your test notification arrived. Rule-based delivery uses this "
                    "connection."
                ),
                3,
            )

    def deliver_one(self, now: datetime) -> bool:
        with self.lock:
            settings = self.store.settings()
            if not settings["delivery_enabled"]:
                return False
            with self.store.connection() as db:
                row = db.execute(
                    (
                        "SELECT * FROM notifications WHERE status='pending' AND "
                        "next_attempt<=? ORDER BY next_attempt, id LIMIT 1"
                    ),
                    (now.isoformat(),),
                ).fetchone()
            if row is None:
                return False
            event = dict(row)
            town_name = event["town_label"] or (
                "No town" if event["town_id"] == "-1" else "Town " + event["town_id"]
            )
            type_name = event["type_label"] or "Type " + event["type_id"]
            try:
                self.publish(
                    settings,
                    event["topic"],
                    f"Illyriad · {town_name} · {type_name}",
                    event["detail"],
                    event["priority"],
                )
                outcome, message, state = "sent", "Accepted by ntfy", "sent"
            except ValidationError as exc:
                outcome, message = "failed", str(exc)
                state = "failed" if event["attempts"] + 1 >= 5 else "pending"
            count = event["attempts"] + 1
            delay = timedelta(minutes=min(60, 2**count))
            with self.store.connection() as db:
                db.execute(
                    (
                        "UPDATE notifications SET status=?, attempts=?, next_attempt=? "
                        "WHERE id=?"
                    ),
                    (state, count, (now + delay).isoformat(), event["id"]),
                )
                db.execute(
                    (
                        "INSERT INTO "
                        "deliveries(notification_id,attempted_at,outcome,message) "
                        "VALUES(?,?,?,?)"
                    ),
                    (event["id"], now.isoformat(), outcome, message),
                )
            return True

    def retry(self, event_id: str, now: datetime) -> bool:
        with self.lock, self.store.connection() as db:
            return bool(
                db.execute(
                    (
                        "UPDATE notifications SET status='pending', attempts=0, "
                        "next_attempt=? WHERE id=? AND status='failed'"
                    ),
                    (now.isoformat(), event_id),
                ).rowcount
            )

    def prune(self, now: datetime) -> None:
        with self.lock:
            cutoff = (
                now - timedelta(days=self.store.settings()["retention_days"])
            ).isoformat()
            with self.store.connection() as db:
                # Import time gives newly discovered old events a full retention window.
                db.execute(
                    (
                        "DELETE FROM notifications WHERE imported_at<? AND status IN "
                        "('history','kept','sent')"
                    ),
                    (cutoff,),
                )
                # seen IDs intentionally survive message retention to prevent replay.

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                now = datetime.now(UTC)
                self.poll(now)
                for _ in range(20):
                    if stop.is_set() or not self.deliver_one(datetime.now(UTC)):
                        break
                self.prune(now)
            except Exception:
                # Raw HTTP errors may contain the API key; never log them.
                with self.store.connection() as db:
                    self.store.set_meta(
                        db,
                        "last_error",
                        (
                            "Background processing failed. Check disk access "
                            "and restart the "
                            "app."
                        ),
                    )
            stop.wait(15)
