"Local administration and the single-process application entry point."

import argparse
import getpass
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn

from .domain import Feed, Notification, Rule
from .service import Service
from .store import Store


def seed_demo(store: Store) -> None:
    if store.metadata().get("initialized"):
        raise SystemExit(
            "Demo data requires an empty installation. Choose another data directory."
        )
    now = datetime.now(UTC)
    examples = [
        (
            "101",
            "1",
            "1",
            "101",
            "Construction complete: Library reaches level 12.",
            "Construction",
            "City",
            "Alderhaven",
        ),
        (
            "102",
            "2",
            "1",
            "102",
            "Research complete: Advanced Masonry is now available.",
            "Research",
            "City",
            "Ravenswatch",
        ),
        (
            "103",
            "3",
            "2",
            "101",
            "Your army has returned to Alderhaven.",
            "Army return",
            "Military",
            "Alderhaven",
        ),
        (
            "104",
            "4",
            "3",
            "103",
            "A caravan has delivered 8,000 stone to your town.",
            "Trade arrival",
            "Trade",
            "Westmere",
        ),
        (
            "105",
            "1",
            "1",
            "103",
            "Construction complete: Storehouse reaches level 18.",
            "Construction",
            "City",
            "Westmere",
        ),
        (
            "106",
            "5",
            "2",
            "102",
            (
                "Military activity reported near Ravenswatch. Review the game for "
                "details."
            ),
            "Military activity",
            "Military",
            "Ravenswatch",
        ),
        (
            "107",
            "2",
            "1",
            "101",
            "Research complete: Siege Encampment.",
            "Research",
            "City",
            "Alderhaven",
        ),
        (
            "108",
            "4",
            "3",
            "102",
            "Your caravan has returned from its trade journey.",
            "Trade arrival",
            "Trade",
            "Ravenswatch",
        ),
    ]
    events = tuple(
        Notification(
            i,
            t,
            c,
            town,
            text,
            (now - timedelta(minutes=index * 19)).isoformat(),
            tl,
            cl,
            name,
        )
        for index, (i, t, c, town, text, tl, cl, name) in enumerate(examples)
    )
    with httpx.Client() as client:
        Service(store, client).ingest(Feed("999999", events), now)
    store.save_rule(
        Rule(
            "Military updates", 10, True, category_ids="2", action="forward", priority=4
        )
    )
    store.save_rule(
        Rule(
            "Capital construction",
            20,
            True,
            type_ids="1",
            town_ids="101",
            action="forward",
        )
    )
    # Simulated statuses for UI review; no network or real credentials.
    with store.connection() as db:
        store.set_meta(db, "demo", "1")
        for event_id, state in (
            ("103", "sent"),
            ("104", "kept"),
            ("106", "failed"),
            ("107", "pending"),
        ):
            db.execute(
                (
                    "UPDATE notifications SET status=?, reason='Simulated demo "
                    "decision' WHERE id=?"
                ),
                (state, event_id),
            )
        db.execute("UPDATE notifications SET is_read=1 WHERE id IN ('104','105','108')")
        db.execute(
            (
                "INSERT INTO "
                "deliveries(notification_id,attempted_at,outcome,message) "
                "VALUES('106',?,'failed','Simulated ntfy connection failure')"
            ),
            (now.isoformat(),),
        )
        db.execute("UPDATE notifications SET attempts=5 WHERE id='106'")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Illyriad Notifier administrator tools"
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path(os.getenv("NOTIFIER_DATA_DIR", ".data"))
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "init", help="Initialize the data volume and administrator password"
    )
    commands.add_parser(
        "reset-password", help="Replace the administrator password and revoke sessions"
    )
    commands.add_parser("seed-demo", help="Add fictional data to an empty installation")
    serve = commands.add_parser("serve", help="Run the UI and background worker")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if args.command in {"init", "reset-password"}:
        password = getpass.getpass("Administrator password (12–200 characters): ")
        if password != getpass.getpass("Confirm password: "):
            raise SystemExit("Passwords did not match.")
        if not 12 <= len(password) <= 200:
            raise SystemExit("Use a password between 12 and 200 characters.")
        store = Store(args.data_dir, password)
        if args.command == "reset-password":
            store.change_password(password)
        print(
            "Administrator ready. Existing passwords are only replaced by "
            "reset-password."
        )
    elif args.command == "seed-demo":
        seed_demo(Store(args.data_dir, os.getenv("NOTIFIER_ADMIN_PASSWORD", "")))
        print(
            "Fictional demo loaded. No messages were sent. Use a fresh volume "
            "for real data."
        )
    else:
        os.environ["NOTIFIER_DATA_DIR"] = str(args.data_dir)
        uvicorn.run(
            "illyriad_notifier.web:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            workers=1,
            access_log=False,
            proxy_headers=False,
        )


if __name__ == "__main__":
    main()
