"""Send the outbox: confirmations and reminders whose time has come.

    uv run cozysetup-outbox run                   # send what's due now, once
    uv run cozysetup-outbox watch                 # check every 60 seconds until Ctrl+C
    uv run cozysetup-outbox watch --every 30
    uv run cozysetup-outbox run --db data/practice/practice.db

Uses the real database by default. No real external channel is connected yet:
terminal and eval messages are written to outbox_delivered.log next to the
database. "Send yourself" messages are left for the owner.
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
from cozysetup.database import DEFAULT_DB_PATH, connect
from cozysetup.senders import LOG_FILE_NAME, default_senders

MARKS = {"sent": "✓ sent", "retry": "↻ retry later", "failed": "✗ failed", "cancelled": "– not sent",
         "error": "⚠ error"}


def show(results: list[outbox.Delivery]) -> None:
    for r in results:
        print(f"  {MARKS[r.outcome]}  {r.message.kind.value} for {r.reference}: {r.detail}")


def summary(results: list[outbox.Delivery]) -> str:
    count = {outcome: sum(r.outcome == outcome for r in results) for outcome in MARKS}
    text = (f"sent {count['sent']} · retry later {count['retry']} · failed {count['failed']} · "
            f"not sent {count['cancelled']}")
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
        senders = senders if senders is not None else default_senders(args.db.parent / LOG_FILE_NAME, service.now)

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


if __name__ == "__main__":
    sys.exit(main())
