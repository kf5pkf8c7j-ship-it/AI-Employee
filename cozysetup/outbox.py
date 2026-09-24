"""The outbox: messages the system sends to customers by itself.

When the owner confirms a booking, a confirmation (the owner's R11) and a
reminder (R12, a few hours before the setup) are queued here - in the same
transaction as the confirmation, so a message exists exactly when the
confirmation does. Sending them is a separate step (the outbox sender).

These functions are called by the BookingService inside its write
transactions; they never commit on their own.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from cozysetup.business_info import BusinessInfo
from cozysetup.database import Language, OutboxKind, OutboxStatus
from cozysetup.replies import booking_values, render

# The channel for bookings the owner entered: no chat to send to, so the
# owner sends these messages personally.
OWNER_CHANNEL = "owner"

# Messages still to be sent (by the system, or by the owner).
WAITING = (OutboxStatus.PENDING, OutboxStatus.SEND_YOURSELF)


@dataclass(frozen=True)
class OutboxMessage:
    id: int
    booking_id: int
    kind: OutboxKind
    channel: str
    recipient: str
    language: Language
    text: str
    send_after: datetime
    status: OutboxStatus
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> OutboxMessage:
        return cls(
            id=row["id"],
            booking_id=row["booking_id"],
            kind=OutboxKind(row["kind"]),
            channel=row["channel"],
            recipient=row["recipient"],
            language=Language(row["language"]),
            text=row["text"],
            send_after=datetime.fromisoformat(row["send_after"]),
            status=OutboxStatus(row["status"]),
            attempts=row["attempts"],
            last_error=row["last_error"],
            created_at=datetime.fromisoformat(row["created_at"]),
            sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
        )


def reminder_time(info: BusinessInfo, booking) -> datetime:
    """When the reminder for this booking is due: the setup start minus the configured hours (Kuwait time)."""
    start = datetime.combine(booking.booking_date, info.pricing.start_time, tzinfo=info.timezone)
    return start - timedelta(hours=info.reminders.hours_before_start)


def queue_confirmation_and_reminder(db: sqlite3.Connection, info: BusinessInfo, booking, now: datetime) -> list[str]:
    """Queue the confirmation (now) and the reminder (on the booking day).

    Returns what was done, for the booking's history. The reminder is skipped
    when its time has already passed - e.g. a same-day booking confirmed late.
    """
    done = [_queue(db, info, booking, OutboxKind.CONFIRMATION, "booking_confirmed", send_after=now, now=now)]
    done.append(queue_reminder(db, info, booking, now))
    return done


def queue_reminder(db: sqlite3.Connection, info: BusinessInfo, booking, now: datetime) -> str:
    due = reminder_time(info, booking)
    if now >= due:
        return f"reminder skipped: confirmed after the reminder time ({due:%d %b %H:%M})"
    return _queue(db, info, booking, OutboxKind.REMINDER, "reminder", send_after=due, now=now)


def cancel_waiting(db: sqlite3.Connection, booking_id: int, now: datetime,
                   kinds: tuple[OutboxKind, ...] = tuple(OutboxKind)) -> int:
    """Cancel messages for this booking that haven't been sent yet. Returns how many."""
    kind_marks = ", ".join("?" for _ in kinds)
    status_marks = ", ".join("?" for _ in WAITING)
    cursor = db.execute(
        f"""
        UPDATE outbox SET status = ?, updated_at = ?
        WHERE booking_id = ? AND kind IN ({kind_marks}) AND status IN ({status_marks})
        """,
        (OutboxStatus.CANCELLED.value, now.isoformat(timespec="seconds"), booking_id,
         *(kind.value for kind in kinds), *(status.value for status in WAITING)),
    )
    return cursor.rowcount


def messages_for(db: sqlite3.Connection, booking_id: int) -> list[OutboxMessage]:
    rows = db.execute("SELECT * FROM outbox WHERE booking_id = ? ORDER BY id", (booking_id,))
    return [OutboxMessage.from_row(row) for row in rows]


def list_messages(db: sqlite3.Connection, statuses: tuple[OutboxStatus, ...] | None = None) -> list[OutboxMessage]:
    """All messages (or only some statuses), in the order they are due."""
    if statuses:
        marks = ", ".join("?" for _ in statuses)
        rows = db.execute(f"SELECT * FROM outbox WHERE status IN ({marks}) ORDER BY send_after, id",
                          [status.value for status in statuses])
    else:
        rows = db.execute("SELECT * FROM outbox ORDER BY send_after, id")
    return [OutboxMessage.from_row(row) for row in rows]


def _queue(db: sqlite3.Connection, info: BusinessInfo, booking, kind: OutboxKind, reply: str,
           *, send_after: datetime, now: datetime) -> str:
    # The owner's approved English wording for now; the customer's language is
    # stored on the booking for when approved Arabic wording exists.
    text = render(info, reply, booking_values(info, booking))["en"]
    if booking.channel == OWNER_CHANNEL:
        status, recipient = OutboxStatus.SEND_YOURSELF, booking.customer_phone
    else:
        status, recipient = OutboxStatus.PENDING, booking.channel_user_id
    stamp = now.isoformat(timespec="seconds")
    db.execute(
        """
        INSERT INTO outbox (booking_id, kind, channel, recipient, language, text, send_after,
                            status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (booking.id, kind.value, booking.channel, recipient, Language.ENGLISH.value, text,
         send_after.isoformat(timespec="seconds"), status.value, stamp, stamp),
    )
    who = "for the owner to send" if status is OutboxStatus.SEND_YOURSELF else f"via {booking.channel}"
    return f"{kind.value} queued {who}, from {send_after:%d %b %H:%M}"
