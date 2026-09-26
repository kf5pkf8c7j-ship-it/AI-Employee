"""Senders: how a message actually reaches a customer on one channel.

The outbox doesn't know how channels work. It looks up the sender for a
message's channel and calls send(). Adding a real channel later (WhatsApp,
SMS, ...) means adding one sender here - nothing else changes.

The LogSender "delivers" by writing the message to a file, so the owner can
see exactly what a customer would have received, and when. The
InstagramSender sends real Instagram DMs: the Instagram worker's chat
replies, and the outbox's confirmations and reminders to Instagram customers.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from cozysetup.check_instagram import GRAPH_URL, TIMEOUT_SECONDS

LOG_FILE_NAME = "outbox_delivered.log"

# Channels without a real connection yet, whose messages are written to the log.
LOG_CHANNELS = ("terminal", "eval")


class SendError(Exception):
    """A sender could not deliver a message.

    permanent=False: worth trying again later (e.g. the network is down).
    permanent=True:  retrying won't help (e.g. the recipient doesn't exist).
    """

    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


class Sender(Protocol):
    def send(self, recipient: str, text: str) -> None:
        """Deliver the text, or raise SendError."""


class LogSender:
    """Writes each delivered message to a file instead of a real channel."""

    def __init__(self, path: Path, channel: str, clock: Callable[[], datetime]):
        self.path = path
        self.channel = channel
        self.clock = clock

    def send(self, recipient: str, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"  {line}" for line in text.splitlines())
        with self.path.open("a", encoding="utf-8") as file:
            file.write(f"{self.clock():%Y-%m-%d %H:%M}  {self.channel} → {recipient}\n{body}\n\n")


# --- Instagram's rules for messages from a business ------------------------------------

INSTAGRAM_CHANNEL = "instagram"
# A business may only message a customer within 24 hours of the customer's last message.
INSTAGRAM_WINDOW = timedelta(hours=24)
# One text message may be at most 1000 bytes (Arabic letters take about 2 bytes each).
INSTAGRAM_MAX_TEXT_BYTES = 1000


def instagram_window_open(db: sqlite3.Connection, customer: str, now: datetime) -> bool:
    """Whether Instagram still lets us message this customer automatically. False when
    we have no record of the conversation or of their last message - we can't be sure."""
    row = db.execute("SELECT last_customer_message_at FROM conversations WHERE channel = ? AND channel_user_id = ?",
                     (INSTAGRAM_CHANNEL, customer)).fetchone()
    if row is None or not row[0]:
        return False
    return now < datetime.fromisoformat(row[0]) + INSTAGRAM_WINDOW


class InstagramSender:
    """Sends a text as an Instagram Direct Message (Instagram API with Instagram Login).

    Returns Instagram's id for the sent message, so its echo can be recognised.
    Instagram only delivers messages within 24 hours of the customer's last message -
    the caller checks that before sending (instagram_window_open). A text over
    1000 bytes is refused here.
    """

    # Meta error codes that mean "busy, try later" (rate limits, temporary problems).
    TEMPORARY_CODES = {1, 2, 4, 17, 32, 341, 613}

    def __init__(self, token: str, post: Callable[[str, dict, str], dict] | None = None):
        self._token = token
        self._post = post or _graph_post

    def send(self, recipient: str, text: str) -> str:
        size = len(text.encode("utf-8"))
        if size > INSTAGRAM_MAX_TEXT_BYTES:
            raise SendError(f"the message is {size} bytes - Instagram allows at most {INSTAGRAM_MAX_TEXT_BYTES}",
                            permanent=True)
        try:
            answer = self._post("me/messages", {"recipient": {"id": recipient}, "message": {"text": text}},
                                self._token)
        except GraphError as error:
            temporary = error.status is None or error.status >= 500 or error.code in self.TEMPORARY_CODES
            raise SendError(str(error), permanent=not temporary) from None
        message_id = answer.get("message_id")
        if not message_id:
            raise SendError(f"Instagram gave no message id: {answer}")
        return str(message_id)


class GraphError(Exception):
    """A failed Instagram API request. status is None when Instagram couldn't be reached."""

    def __init__(self, message: str, status: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def _graph_post(path: str, body: dict, token: str) -> dict:
    """One POST to the Instagram Graph API. The token goes in a header, and never into errors."""
    request = urllib.request.Request(
        f"{GRAPH_URL}/{path}", data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("error", {})
        except (ValueError, UnicodeDecodeError):
            detail = {}
        message = str(detail.get("message") or f"HTTP {error.code}").replace(token, "…")
        raise GraphError(f"Instagram refused the message: {message}", error.code, detail.get("code")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = str(getattr(error, "reason", error)).replace(token, "…")
        raise GraphError(f"Could not reach Instagram ({reason})") from None


def default_senders(log_path: Path, clock: Callable[[], datetime],
                    instagram_token: str | None = None) -> dict[str, Sender]:
    """Which sender handles which channel. A channel missing here can't be sent to:
    its messages fail, and the owner gets a handoff. Instagram is included when
    there is an access token."""
    senders: dict[str, Sender] = {channel: LogSender(log_path, channel, clock) for channel in LOG_CHANNELS}
    if instagram_token:
        senders[INSTAGRAM_CHANNEL] = InstagramSender(instagram_token)
    return senders
