"""How the Instagram connection is doing - read from the database, changes nothing.

Shown by `cozysetup-admin overview` and `cozysetup-instagram status`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from cozysetup.database import InboundStatus, ReplyStatus
from cozysetup.senders import INSTAGRAM_CHANNEL

WORKER_HEARTBEAT = "instagram-worker"
# The worker checks every few seconds; no round for this long means it has stopped (or is stuck).
WORKER_SILENT_AFTER = timedelta(minutes=2)
NOT_ON_TEST_LIST_MARK = "not on the test list%"


@dataclass(frozen=True)
class InstagramStatus:
    conversations: int
    last_message_at: datetime | None          # the last message the webhook recorded
    worker_last_round_at: datetime | None
    worker_details: str
    worker_silent: bool                        # no round for a while (or never)
    waiting: int                               # to be answered by the AI
    oldest_waiting_at: datetime | None
    not_on_test_list: int                      # waiting, but the AI may not answer them (test mode)
    failed_messages: int                       # could not be answered - handed to the owner
    paused: list[str]                          # who, for each conversation where the owner took over
    replies_pending: int
    replies_failed: int
    replies_send_yourself: int

    @property
    def in_use(self) -> bool:
        return bool(self.conversations or self.last_message_at or self.worker_last_round_at)

    def lines(self, when) -> list[str]:
        """What the owner reads. `when` formats a time."""
        lines = []
        if self.worker_last_round_at is None:
            lines.append("⚠ The Instagram worker has never run on this database - DMs are not answered.")
        elif self.worker_silent:
            lines.append(f"⚠ The Instagram worker's last round was {when(self.worker_last_round_at)} - "
                         "is it still running? (cozysetup-instagram work)")
        else:
            lines.append(f"Worker running - last round {when(self.worker_last_round_at)} ({self.worker_details})")
        lines.append(f"Last DM received: {when(self.last_message_at) if self.last_message_at else 'none yet'}")
        if self.waiting:
            lines.append(f"{self.waiting} message(s) waiting to be answered, oldest from {when(self.oldest_waiting_at)}")
        if self.not_on_test_list:
            lines.append(f"⚠ {self.not_on_test_list} message(s) from people not on the test list - "
                         "answer them yourself in Instagram")
        if self.failed_messages:
            lines.append(f"⚠ {self.failed_messages} message(s) could not be answered - see: handoffs")
        if self.replies_failed or self.replies_send_yourself:
            lines.append(f"⚠ {self.replies_failed + self.replies_send_yourself} AI reply part(s) not delivered "
                         "- see: handoffs")
        if self.replies_pending:
            lines.append(f"{self.replies_pending} AI reply part(s) waiting to be sent")
        for who in self.paused:
            lines.append(f"⏸ AI paused for {who} - you are handling it (resume: cozysetup-admin resume ...)")
        return lines

    @property
    def needs_attention(self) -> bool:
        return bool(self.worker_silent or self.not_on_test_list or self.failed_messages
                    or self.replies_failed or self.replies_send_yourself)


def instagram_status(db: sqlite3.Connection, now: datetime) -> InstagramStatus:
    def one(sql: str, *values):
        return db.execute(sql, values).fetchone()[0]

    def moment(value):
        return datetime.fromisoformat(value) if value else None

    waiting_filter = "channel = ? AND status = ?"
    heartbeat = db.execute("SELECT last_round_at, details FROM heartbeats WHERE program = ?",
                           (WORKER_HEARTBEAT,)).fetchone()
    last_round = moment(heartbeat["last_round_at"]) if heartbeat else None
    replies = dict(db.execute(
        "SELECT r.status, COUNT(*) FROM conversation_replies r JOIN conversations c ON c.id = r.conversation_id "
        "WHERE c.channel = ? GROUP BY r.status", (INSTAGRAM_CHANNEL,)).fetchall())
    paused_rows = db.execute("SELECT channel_user_id, username, display_name FROM conversations "
                             "WHERE channel = ? AND ai_paused = 1 ORDER BY paused_at", (INSTAGRAM_CHANNEL,))
    return InstagramStatus(
        conversations=one("SELECT COUNT(*) FROM conversations WHERE channel = ?", INSTAGRAM_CHANNEL),
        last_message_at=moment(one("SELECT MAX(received_at) FROM inbound_messages WHERE channel = ?",
                                   INSTAGRAM_CHANNEL)),
        worker_last_round_at=last_round,
        worker_details=heartbeat["details"] if heartbeat else "",
        worker_silent=last_round is None or now - last_round > WORKER_SILENT_AFTER,
        waiting=one(f"SELECT COUNT(*) FROM inbound_messages WHERE {waiting_filter} "
                    "AND (error IS NULL OR error NOT LIKE ?)",
                    INSTAGRAM_CHANNEL, InboundStatus.WAITING.value, NOT_ON_TEST_LIST_MARK),
        oldest_waiting_at=moment(one(f"SELECT MIN(received_at) FROM inbound_messages WHERE {waiting_filter}",
                                     INSTAGRAM_CHANNEL, InboundStatus.WAITING.value)),
        not_on_test_list=one(f"SELECT COUNT(*) FROM inbound_messages WHERE {waiting_filter} AND error LIKE ?",
                             INSTAGRAM_CHANNEL, InboundStatus.WAITING.value, NOT_ON_TEST_LIST_MARK),
        failed_messages=one(f"SELECT COUNT(*) FROM inbound_messages WHERE {waiting_filter}",
                            INSTAGRAM_CHANNEL, InboundStatus.FAILED.value),
        paused=[who(row) for row in paused_rows],
        replies_pending=replies.get(ReplyStatus.PENDING.value, 0),
        replies_failed=replies.get(ReplyStatus.FAILED.value, 0),
        replies_send_yourself=replies.get(ReplyStatus.SEND_YOURSELF.value, 0),
    )


def who(row) -> str:
    """@username (Name), or the Instagram id while the username isn't known."""
    if row["username"]:
        return f"@{row['username']}" + (f" ({row['display_name']})" if row["display_name"] else "")
    return f"Instagram id {row['channel_user_id']}"


def instagram_customer(db: sqlite3.Connection, customer_id: str) -> str:
    """How the owner recognises an Instagram customer, from their Instagram id."""
    row = db.execute("SELECT channel_user_id, username, display_name FROM conversations "
                     "WHERE channel = ? AND channel_user_id = ?", (INSTAGRAM_CHANNEL, customer_id)).fetchone()
    return who(row) if row else f"Instagram id {customer_id}"
