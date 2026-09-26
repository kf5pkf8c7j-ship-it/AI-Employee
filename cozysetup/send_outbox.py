"""Send the outbox: confirmations and reminders whose time has come.

    uv run cozysetup-outbox run                   # send what's due now, once
    uv run cozysetup-outbox watch                 # check every 60 seconds until Ctrl+C
    uv run cozysetup-outbox watch --every 30
    uv run cozysetup-outbox run --db data/practice/practice.db

Uses the real database by default. Instagram messages are sent as real
Instagram DMs (with the token in .env) - but only within Instagram's 24-hour
window; otherwise they become "send yourself" for the owner. Terminal and
eval messages are written to outbox_delivered.log next to the database.
"Send yourself" messages are left for the owner.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from cozysetup import outbox
from cozysetup.bookings import BookingService
from cozysetup.business_info import BusinessInfoError, load_business_info
from cozysetup.database import DEFAULT_DB_PATH, OutboxStatus, connect
from cozysetup.senders import INSTAGRAM_CHANNEL, LOG_FILE_NAME, default_senders
from cozysetup.settings import MissingApiKey, load_instagram_token

MARKS = {"sent": "✓ sent", "retry": "↻ retry later", "failed": "✗ failed", "cancelled": "– not sent",
         "send_yourself": "✋ send yourself", "error": "⚠ error"}


def show(results: list[outbox.Delivery]) -> None:
    for r in results:
        print(f"  {MARKS[r.outcome]}  {r.message.kind.value} for {r.reference}: {r.detail}")


def summary(results: list[outbox.Delivery]) -> str:
    count = {outcome: sum(r.outcome == outcome for r in results) for outcome in MARKS}
    text = (f"sent {count['sent']} · retry later {count['retry']} · failed {count['failed']} · "
            f"not sent {count['cancelled']}")
    if count["send_yourself"]:
        text += (f" · send yourself {count['send_yourself']} (Instagram's 24-hour window is closed - "
                 "see: cozysetup-admin outbox)")
    if count["error"]:
        text += f" · errors {count['error']} (those messages stay pending and are tried again next run)"
    if count["failed"]:
        text += " (a handoff was created for each failure - see: cozysetup-admin handoffs)"
    return text


def main(
    argv: list[str] | None = None,
    *,
    senders: dict | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_rounds: int | None = None,
) -> int:
    """`senders`, `clock`, `sleep` and `max_rounds` let tests use pretend senders,
    a fixed time, no waiting, and a limited number of watch rounds."""
    parser = argparse.ArgumentParser(prog="cozysetup-outbox",
                                     description="Send confirmations and reminders whose time has come.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH,
                        help="database to use (default: the real one)")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    commands.add_parser("run", help="send what's due now, once")
    watch = commands.add_parser("watch", help="keep checking until Ctrl+C")
    watch.add_argument("--every", type=int, default=60, help="seconds between checks (default 60)")
    args = parser.parse_args(argv)

    try:
        info = load_business_info()
    except BusinessInfoError as error:
        print(error, file=sys.stderr)
        return 1

    db = connect(args.db)
    try:
        service = BookingService(db, info, clock=clock, proofs_dir=args.db.parent / "payment_proofs")
        if senders is None:
            senders = default_senders(args.db.parent / LOG_FILE_NAME, service.now, _instagram_token(db))

        if args.command == "run":
            results = outbox.deliver_due(service, senders)
            show(results)
            print(summary(results) if results else "Nothing due.")
            return 2 if any(r.outcome in ("failed", "error") for r in results) else 0

        print(f"Watching the outbox every {args.every} seconds - press Ctrl+C to stop.")
        rounds = 0
        try:
            while max_rounds is None or rounds < max_rounds:
                rounds += 1
                try:
                    results = outbox.deliver_due(service, senders)
                except Exception as error:
                    # An unexpected problem in this round must not stop the watcher:
                    # report it and try again next round.
                    print(f"{service.now():%H:%M}  ⚠ this round failed: {type(error).__name__}: {error} "
                          "- trying again next round", file=sys.stderr)
                    results = []
                if results:   # only speak when something happened
                    print(f"{service.now():%H:%M}")
                    show(results)
                    print(f"  {summary(results)}")
                if max_rounds is None or rounds < max_rounds:
                    sleep(args.every)
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0
    finally:
        db.close()


def _instagram_token(db) -> str | None:
    """The Instagram access token from .env - or None, with a note when Instagram
    messages are waiting (they then fail and the owner gets a handoff)."""
    try:
        return load_instagram_token()
    except MissingApiKey:
        waiting = db.execute("SELECT COUNT(*) FROM outbox WHERE channel = ? AND status = ?",
                             (INSTAGRAM_CHANNEL, OutboxStatus.PENDING.value)).fetchone()[0]
        if waiting:
            print(f"⚠ No IG_ACCESS_TOKEN in .env: {waiting} waiting Instagram message(s) can't be sent automatically.",
                  file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
