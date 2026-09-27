"""Run the Instagram connection.

    uv run cozysetup-instagram serve                 # receive DMs at http://127.0.0.1:8000
    uv run cozysetup-instagram serve --port 8080
    uv run cozysetup-instagram work --only @tester1,@tester2   # answer ONLY these accounts (test mode)
    uv run cozysetup-instagram work --only @tester1 --once     # answer what's waiting now, once
    uv run cozysetup-instagram work --answer-everyone          # the real launch: answer every customer
    uv run cozysetup-instagram status                # how it's going: waiting, failed, paused, worker running?
    uv run cozysetup-instagram conversations         # list conversations (and which are paused)
    uv run cozysetup-instagram resume @username      # let the AI answer again after the owner took over

Every command takes --db (default: the real database), e.g.
    uv run cozysetup-instagram work --db data/practice/practice.db

"serve" listens on this computer only (127.0.0.1). Meta reaches it through an
HTTPS tunnel (e.g. Cloudflare Tunnel) pointing at this address. It only records
incoming DMs. "work" answers them with the AI and sends the replies - so it
sends real Instagram messages. It refuses to start unless told who it may
answer: --only @usernames (test mode - nobody else ever gets an AI reply), or
--answer-everyone.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import openai
import uvicorn

from cozysetup.bookings import BookingService
from cozysetup.business_info import BusinessInfoError, load_business_info
from cozysetup.conversations import ConversationStore, normalize_username
from cozysetup.database import DEFAULT_DB_PATH, connect
from cozysetup.instagram_webhook import create_app
from cozysetup.instagram_status import instagram_status
from cozysetup.instagram_worker import CHANNEL, InstagramProfiles, InstagramWorker, Outcome, download_image
from cozysetup.senders import InstagramSender
from cozysetup.settings import MissingApiKey, load_api_key, load_instagram_token, load_webhook_settings

MARKS = {"answered": "✓", "sent": "→", "echo": "·", "recorded": "⏸", "owner_replied": "⏸", "ignored": "–",
         "cancelled": "–", "retry": "↻", "failed": "✗", "send_yourself": "✋", "not_answered": "⊘",
         "marked_sent": "✓"}


def main(
    argv: list[str] | None = None,
    *,
    client=None,
    sender=None,
    profiles=None,
    download: Callable[[str], bytes] = download_image,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_rounds: int | None = None,
) -> int:
    """The keyword arguments let tests use a pretend model and sender, a fixed time and no waiting."""
    parser = argparse.ArgumentParser(prog="cozysetup-instagram", description="The Instagram connection.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    serve = commands.add_parser("serve", help="receive Instagram DMs and record them")
    serve.add_argument("--host", default="127.0.0.1", help="default: this computer only")
    serve.add_argument("--port", type=int, default=8000)
    work = commands.add_parser("work", help="answer recorded DMs with the AI and send the replies")
    who = work.add_mutually_exclusive_group(required=True)
    who.add_argument("--only", type=usernames, metavar="@USER,@USER",
                     help="answer ONLY these Instagram accounts (test mode); nobody else gets an AI reply")
    who.add_argument("--answer-everyone", action="store_true", help="answer every customer (the real launch)")
    work.add_argument("--once", action="store_true", help="one round, then stop")
    work.add_argument("--every", type=int, default=5, help="seconds between rounds (default 5)")
    status = commands.add_parser("status", help="how it's going: waiting, failed, paused - is the worker running?")
    commands.add_parser("conversations", help="list Instagram conversations")
    resume = commands.add_parser("resume", help="let the AI answer again in a paused conversation")
    resume.add_argument("customer", help="@username (or the Instagram id) - see: conversations")
    for command in (serve, work, status, commands.choices["conversations"], resume):
        command.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="default: the real database")
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    try:
        info = load_business_info()
    except BusinessInfoError as error:
        print(error, file=sys.stderr)
        return 1
    db = connect(args.db)
    try:
        service = BookingService(db, info, clock=clock, proofs_dir=args.db.parent / "payment_proofs")
        store = ConversationStore(service, args.db.parent / "conversation_attachments")
        if args.command == "status":
            return _status(db, service)
        if args.command == "conversations":
            return _conversations(store)
        if args.command == "resume":
            return _resume(store, args.customer)
        try:
            client = client if client is not None else openai.OpenAI(api_key=load_api_key())
            if sender is None or profiles is None:
                token = load_instagram_token()
                sender = sender if sender is not None else InstagramSender(token)
                profiles = profiles if profiles is not None else InstagramProfiles(token)
        except MissingApiKey as error:
            print(f"✗ {error}", file=sys.stderr)
            return 1
        only = None if args.answer_everyone else args.only
        worker = InstagramWorker(service, store, client, sender, download=download, profiles=profiles,
                                 only=only, log_dir=args.db.parent / "conversations")
        return _work(worker, service, args, sleep, max_rounds)
    finally:
        db.close()


def _serve(args) -> int:
    try:
        settings = load_webhook_settings()
    except MissingApiKey as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1
    connect(args.db).close()   # create or upgrade the database before the first message arrives

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    print(f"Receiving Instagram DMs at http://{args.host}:{args.port}/webhooks/instagram  (database: {args.db})")
    print("Messages are only recorded here - run 'cozysetup-instagram work' to answer them. Press Ctrl+C to stop.")
    uvicorn.run(create_app(settings, args.db), host=args.host, port=args.port, log_level="warning")
    return 0


def _work(worker: InstagramWorker, service: BookingService, args, sleep, max_rounds) -> int:
    if args.once:
        results = worker.run_once()
        show(service, results)
        if not results:
            print("Nothing to do.")
        return 2 if any(r.what == "failed" for r in results) else 0

    who = ("EVERY customer" if worker.only is None
           else "ONLY " + ", ".join(f"@{name}" for name in sorted(worker.only)) + " (test mode)")
    print(f"Answering Instagram DMs from {who} (database: {args.db}), checking every {args.every} seconds. "
          "Replies are sent for real. Press Ctrl+C to stop.")
    rounds = 0
    try:
        while max_rounds is None or rounds < max_rounds:
            rounds += 1
            try:
                show(service, worker.run_once())
            except Exception as error:
                # A problem in one round must not stop the worker: report it and try again.
                print(f"{service.now():%H:%M:%S}  ⚠ this round failed: {type(error).__name__}: {error} "
                      "- trying again next round", file=sys.stderr)
            if max_rounds is None or rounds < max_rounds:
                sleep(args.every)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def show(service: BookingService, results: list[Outcome]) -> None:
    for r in results:
        print(f"{service.now():%H:%M:%S}  {MARKS.get(r.what, '?')} {r.what:<13} {r.customer}  {r.detail}")


def usernames(text: str) -> set[str]:
    """ "@sara.k, @ahmad" -> {"sara.k", "ahmad"}. An empty list is refused: test mode
    must name at least one account."""
    names = {normalize_username(part) for part in text.split(",") if normalize_username(part)}
    if not names:
        raise argparse.ArgumentTypeError("name at least one @username")
    return names


def _status(db, service: BookingService) -> int:
    status = instagram_status(db, service.now())
    for line in status.lines(lambda moment: f"{moment:%a %d %b %H:%M:%S}"):
        print(line)
    return 1 if status.needs_attention else 0


def _conversations(store: ConversationStore) -> int:
    conversations = store.all(CHANNEL)
    if not conversations:
        print("No Instagram conversations yet.")
    for stored in conversations:
        state = f"⏸ AI paused since {stored.paused_at:%d %b %H:%M} ({stored.pause_reason})" if stored.ai_paused \
            else "AI answering"
        last = f"{stored.last_customer_message_at:%d %b %H:%M}" if stored.last_customer_message_at else "-"
        print(f"{stored.who}  language: {stored.language.value if stored.language else '?'}  "
              f"last customer message: {last}  {state}")
    return 0


def _resume(store: ConversationStore, customer: str) -> int:
    stored = store.find(CHANNEL, customer)
    if stored is None:
        print(f"✗ No Instagram conversation with {customer}", file=sys.stderr)
        return 1
    if not stored.ai_paused:
        print(f"The AI is already answering {stored.who}.")
        return 0
    store.resume_ai(stored)
    print(f"✓ The AI answers {stored.who} again, from their next message.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
