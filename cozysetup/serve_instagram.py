"""Run the Instagram webhook receiver.

    uv run cozysetup-instagram serve                 # http://127.0.0.1:8000
    uv run cozysetup-instagram serve --port 8080
    uv run cozysetup-instagram serve --db data/practice/practice.db

It listens on this computer only (127.0.0.1). Meta reaches it through an HTTPS
tunnel (e.g. Cloudflare Tunnel) pointing at this address. It only records
incoming DMs - it does not reply (that is Step 7.4).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from cozysetup.database import DEFAULT_DB_PATH, connect
from cozysetup.instagram_webhook import create_app
from cozysetup.settings import MissingApiKey, load_webhook_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cozysetup-instagram", description="The Instagram webhook receiver.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    serve = commands.add_parser("serve", help="receive Instagram DMs and record them")
    serve.add_argument("--host", default="127.0.0.1", help="default: this computer only")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="default: the real database")
    args = parser.parse_args(argv)

    try:
        settings = load_webhook_settings()
    except MissingApiKey as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1
    connect(args.db).close()   # create or upgrade the database before the first message arrives

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    print(f"Receiving Instagram DMs at http://{args.host}:{args.port}/webhooks/instagram  (database: {args.db})")
    print("Messages are only recorded - replies come in Step 7.4. Press Ctrl+C to stop.")
    uvicorn.run(create_app(settings, args.db), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
