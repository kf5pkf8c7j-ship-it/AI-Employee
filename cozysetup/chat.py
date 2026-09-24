"""Talk to the AI employee in the terminal - you play the customer.

    uv run cozysetup-chat
    uv run cozysetup-chat --db data/practice/practice.db     (the default)

Uses a practice database by default, so no real bookings are created.
Check what the AI did with the owner commands, e.g.:
    uv run cozysetup-admin --db data/practice/practice.db pending

In the chat:  /image PATH   send an image (e.g. a transfer screenshot)
              /new          start a new conversation (another customer)
              /quit         leave
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import openai

from cozysetup.agent import Agent, AgentReply, ConversationLog, Usage
from cozysetup.bookings import BookingService
from cozysetup.business_info import BusinessInfoError, load_business_info
from cozysetup.database import DEFAULT_DB_PATH, connect
from cozysetup.settings import MODEL, MissingApiKey, load_api_key
from cozysetup.tools import Conversation

PRACTICE_DB_PATH = DEFAULT_DB_PATH.parent / "practice" / "practice.db"
GREY, RESET = "\033[90m", "\033[0m"

HELP = """\
You are the customer. Write as a customer would (English, Kuwaiti Arabic or Arabizi).
  /image PATH   send an image, e.g. a transfer screenshot
  /new          start a new conversation (another customer)
  /quit         leave"""


def main(
    argv: list[str] | None = None,
    *,
    client=None,
    ask: Callable[[str], str] = input,
    clock: Callable[[], datetime] | None = None,
) -> int:
    """`client`, `ask` and `clock` let tests use a pretend model, pretend typing and a fixed time."""
    parser = argparse.ArgumentParser(prog="cozysetup-chat", description="Talk to the AI employee as a customer.")
    parser.add_argument("--db", type=Path, default=PRACTICE_DB_PATH,
                        help="database to use (default: the practice database)")
    args = parser.parse_args(argv)

    try:
        info = load_business_info()
    except BusinessInfoError as error:
        print(error, file=sys.stderr)
        return 1
    if client is None:
        try:
            client = openai.OpenAI(api_key=load_api_key())
        except MissingApiKey as error:
            print(f"✗ {error}", file=sys.stderr)
            return 1

    db = connect(args.db)
    service = BookingService(db, info, clock=clock, proofs_dir=args.db.parent / "payment_proofs")
    log_dir = args.db.parent / "conversations"
    practice = "practice database" if args.db == PRACTICE_DB_PATH else f"database {args.db}"
    print(f"CozySetup AI employee - {MODEL} - {practice}")
    print(HELP)

    agent, total = _new_conversation(client, service, log_dir), Usage()
    try:
        while True:
            try:
                line = ask("\nYou: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line == "/quit":
                break
            if line == "/new":
                agent = _new_conversation(client, service, log_dir)
                print(f"{GREY}(new conversation - log: {agent.log.path.name}){RESET}")
                continue

            images: list[bytes] = []
            if line.startswith("/image"):
                path = Path(line.removeprefix("/image").strip()).expanduser()
                if not path.is_file():
                    print(f"{GREY}(no such file: {path}){RESET}")
                    continue
                images, line = [path.read_bytes()], ""

            reply = agent.reply(line, images=images)
            _add(total, reply.usage)
            print(f"\nCozySetup: {reply.text}")
            print(f"{GREY}{_details(reply)}{RESET}")
    finally:
        db.close()
        if total.requests:
            print(f"{GREY}Total this session: {total.requests} requests, about ${total.cost_usd(MODEL):.4f}{RESET}")
    return 0


def _new_conversation(client, service: BookingService, log_dir: Path) -> Agent:
    started = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    conversation = Conversation(channel="terminal", channel_user_id=f"terminal-{started}")
    log = ConversationLog(log_dir / f"{started}_terminal.jsonl")
    return Agent(client, service, conversation, log=log)


def _details(reply: AgentReply) -> str:
    usage = reply.usage
    parts = [
        f"{usage.requests} request(s)",
        f"{usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens:,} tokens in "
        f"({usage.cache_read_tokens:,} cached)",
        f"{usage.output_tokens:,} out",
        f"about ${usage.cost_usd(MODEL):.4f}",
    ]
    if reply.tools_used:
        parts.append("tools: " + ", ".join(reply.tools_used))
    if reply.handed_off_after_problem:
        parts.append("⚠ problem - handed to the owner (see the log)")
    return "(" + " · ".join(parts) + ")"


def _add(total: Usage, usage: Usage) -> None:
    total.input_tokens += usage.input_tokens
    total.output_tokens += usage.output_tokens
    total.cache_read_tokens += usage.cache_read_tokens
    total.cache_write_tokens += usage.cache_write_tokens
    total.requests += usage.requests


if __name__ == "__main__":
    sys.exit(main())
