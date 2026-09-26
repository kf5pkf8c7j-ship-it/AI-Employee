"""The Instagram webhook receiver: Meta -> our server -> the database.

Meta calls us whenever someone sends a Direct Message to @cozysetup.kw.
This receiver only checks and records. It never replies, never calls the AI
and never downloads anything - that happens afterwards (Step 7.4), so Meta
always gets a quick answer.

    GET  /webhooks/instagram   Meta's one-time verification (hub.challenge)
    POST /webhooks/instagram   incoming messages - signed by Meta
    GET  /health               "ok" - to check the server is running
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from cozysetup.database import connect
from cozysetup.settings import WebhookSettings

CHANNEL = "instagram"
MAX_BODY_BYTES = 1_000_000        # Meta's messaging events are a few KB; anything this big is not from Meta
SIGNATURE_HEADER = "X-Hub-Signature-256"

log = logging.getLogger("cozysetup.instagram")


# --- Checking that a request really comes from Meta ---------------------------------

def signature_is_valid(app_secret: str, body: bytes, header: str | None) -> bool:
    """X-Hub-Signature-256 is "sha256=" + an HMAC-SHA256 of the exact raw body, made
    with the app secret. Compared in constant time so the timing reveals nothing."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


# --- Reading Meta's events -------------------------------------------------------------

@dataclass(frozen=True)
class Inbound:
    external_id: str        # Instagram's message id (mid) - unique
    customer_id: str        # the customer's Instagram-scoped id (IGSID)
    is_echo: bool           # sent FROM our account (by the owner in the app, or by our system)
    event: dict             # the event exactly as received


@dataclass
class Reading:
    messages: list[Inbound] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def read_events(payload: dict, account_id: str) -> Reading:
    """Every message event for our account. Read receipts, reactions, deleted
    messages, button clicks and other accounts' events are skipped."""
    reading = Reading()
    for entry in payload.get("entry") or []:
        # Messages come as "messaging" events; accept the "changes" form too, in case Meta sends it that way.
        events = list(entry.get("messaging") or [])
        events += [change.get("value") or {} for change in entry.get("changes") or []
                   if change.get("field") == "messages"]
        if str(entry.get("id")) != account_id:
            for _ in events:
                reading.skip("another account")
            continue
        for event in events:
            message = event.get("message")
            if not isinstance(message, dict):
                reading.skip("not a message (read receipt, reaction, click…)")
                continue
            if message.get("is_deleted"):
                reading.skip("deleted message")
                continue
            mid, sender, recipient = message.get("mid"), (event.get("sender") or {}).get("id"), \
                (event.get("recipient") or {}).get("id")
            if not mid or not sender or not recipient:
                reading.skip("incomplete message")
                continue
            echo = bool(message.get("is_echo"))
            # For an echo the customer is the recipient; otherwise the sender.
            reading.messages.append(Inbound(str(mid), str(recipient if echo else sender), echo, event))
    return reading


def store(db, messages: list[Inbound], received_at: datetime) -> tuple[int, int]:
    """Record each message once. Returns (new, duplicates). All or nothing."""
    new = 0
    with db:   # one transaction
        for message in messages:
            # Only a repeated message id is skipped. Any other problem raises, the whole
            # batch is rolled back, and Meta retries - nothing is dropped silently.
            # (INSERT OR IGNORE would also silently skip e.g. a missing customer id.)
            new += db.execute(
                "INSERT INTO inbound_messages (channel, external_id, channel_user_id, received_at, payload) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (channel, external_id) DO NOTHING",
                (CHANNEL, message.external_id, message.customer_id, received_at.isoformat(timespec="seconds"),
                 json.dumps(message.event, ensure_ascii=False)),
            ).rowcount
    return new, len(messages) - new


# --- The web app ---------------------------------------------------------------------------

def create_app(settings: WebhookSettings, db_path: Path, clock: Callable[[], datetime] | None = None) -> Starlette:
    """`clock` lets tests fix the time; normally the real (local) time is used."""
    now = clock or (lambda: datetime.now().astimezone())

    async def verify(request: Request) -> PlainTextResponse:
        params = request.query_params
        if params.get("hub.mode") == "subscribe" and hmac.compare_digest(
                params.get("hub.verify_token", ""), settings.verify_token) and params.get("hub.challenge"):
            log.info("webhook verified by Meta")
            return PlainTextResponse(params["hub.challenge"])
        log.warning("webhook verification refused")
        return PlainTextResponse("Forbidden", status_code=403)

    async def receive(request: Request) -> PlainTextResponse:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return PlainTextResponse("Too large", status_code=413)
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return PlainTextResponse("Too large", status_code=413)

        # Checked on the raw bytes, before anything else looks at them.
        if not signature_is_valid(settings.app_secret, body, request.headers.get(SIGNATURE_HEADER)):
            log.warning("request with a missing or invalid signature refused")
            return PlainTextResponse("Forbidden", status_code=403)

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return PlainTextResponse("Bad request", status_code=400)
        if not isinstance(payload, dict):
            return PlainTextResponse("Bad request", status_code=400)

        reading = read_events(payload, settings.account_id)
        try:
            db = connect(db_path)
            try:
                new, duplicates = store(db, reading.messages, now())
            finally:
                db.close()
        except Exception:
            # Nothing was saved (the transaction was rolled back): answer 500 so Meta tries again.
            log.exception("could not save incoming messages - Meta will retry")
            return PlainTextResponse("Try again", status_code=500)

        if reading.messages or reading.skipped:
            skipped = ", ".join(f"{count} {reason}" for reason, count in reading.skipped.items()) or "none"
            log.info("received %d new, %d duplicate; skipped: %s", new, duplicates, skipped)
        return PlainTextResponse("EVENT_RECEIVED")

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(routes=[
        Route("/webhooks/instagram", verify, methods=["GET"]),
        Route("/webhooks/instagram", receive, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ])
