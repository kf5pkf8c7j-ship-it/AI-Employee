"""Senders: how a message actually reaches a customer on one channel.

The outbox doesn't know how channels work. It looks up the sender for a
message's channel and calls send(). Adding a real channel later (WhatsApp,
SMS, ...) means adding one sender here - nothing else changes.

No real external channel is connected yet. The LogSender "delivers" by
writing the message to a file, so the owner can see exactly what a customer
would have received, and when.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol

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


def default_senders(log_path: Path, clock: Callable[[], datetime]) -> dict[str, Sender]:
    """Which sender handles which channel. A channel missing here can't be sent to:
    its messages fail, and the owner gets a handoff."""
    return {channel: LogSender(log_path, channel, clock) for channel in LOG_CHANNELS}
