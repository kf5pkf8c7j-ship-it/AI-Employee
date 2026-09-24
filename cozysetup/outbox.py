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
from cozysetup.database import BookingStatus, Language, OutboxKind, OutboxStatus
from cozysetup.replies import booking_values, render
from cozysetup.senders import SendError

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
    updated_at: datetime | None = None

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
            updated_at=datetime.fromisoformat(row["updated_at"]),
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


def get_message(db: sqlite3.Connection, message_id: int) -> OutboxMessage | None:
    row = db.execute("SELECT * FROM outbox WHERE id = ?", (message_id,)).fetchone()
    return OutboxMessage.from_row(row) if row else None


def next_retry(message: OutboxMessage) -> datetime | None:
    """When a failed-but-retrying message will be tried again."""
    if message.status is not OutboxStatus.PENDING or not message.attempts or not message.updated_at:
        return None
    return message.updated_at + RETRY_WAITS[min(message.attempts, len(RETRY_WAITS)) - 1]


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


# --- The owner marks a message as sent ------------------------------------------------

class OutboxRefused(Exception):
    """The owner asked for something the outbox rules don't allow."""


# The owner may mark these as sent: messages only the owner sends, and ones the
# automatic sender gave up on (the owner then contacted the customer personally).
OWNER_CAN_MARK_SENT = (OutboxStatus.SEND_YOURSELF, OutboxStatus.FAILED)


def mark_sent_by_owner(service, message_id: int, note: str = "") -> OutboxMessage:
    """The owner sent this message personally. Checked again inside the transaction."""
    with service.write_transaction():
        message = get_message(service.db, message_id)
        if message is None:
            raise OutboxRefused(f"No message #{message_id}")
        if message.status not in OWNER_CAN_MARK_SENT:
            reason = {
                OutboxStatus.PENDING: "the automatic sender handles it",
                OutboxStatus.SENT: "it was already sent",
                OutboxStatus.CANCELLED: "it was cancelled",
            }[message.status]
            raise OutboxRefused(f"Message #{message_id} can't be marked as sent: {reason}")
        booking = service.get_booking_by_id(message.booking_id)
        if booking.status is not BookingStatus.CONFIRMED:
            raise OutboxRefused(f"Message #{message_id} can't be marked as sent: "
                                f"{booking.reference} is {booking.status.value}")
        stamp = service.now().isoformat(timespec="seconds")
        service.db.execute("UPDATE outbox SET status = ?, sent_at = ?, updated_at = ? WHERE id = ?",
                           (OutboxStatus.SENT.value, stamp, stamp, message.id))
        how = " after automatic sending failed" if message.status is OutboxStatus.FAILED else ""
        service.add_owner_history(booking.id, "outbox",
                                  f"{message.kind.value} sent by the owner{how}" + (f" ({note})" if note else ""))
    return get_message(service.db, message_id)


# --- Delivering due messages (the outbox sender) -------------------------------------
#
# Each due message is handled on its own:
#   1. claim it and re-check its booking     (one transaction)
#   2. send it through its channel's sender  (outside the database)
#   3. record the result                     (one transaction, with history / handoff)
# A crash between 2 and 3 means the message is sent again next time: a rare
# duplicate is better than a customer message that is lost ("at least once").

MAX_ATTEMPTS = 3
# How long to wait after a failed attempt before the next one: after the 1st, after the 2nd.
RETRY_WAITS = (timedelta(minutes=5), timedelta(minutes=30))


@dataclass(frozen=True)
class Delivery:
    message: OutboxMessage
    reference: str
    outcome: str         # "sent", "retry", "failed", "cancelled" or "error"
    detail: str


def deliver_due(service, senders: dict, now: datetime | None = None) -> list[Delivery]:
    """Send every pending message whose time has come. Returns what happened to each."""
    now = now or service.now()
    rows = service.db.execute(
        "SELECT * FROM outbox WHERE status = ? AND send_after <= ? ORDER BY send_after, id",
        (OutboxStatus.PENDING.value, now.isoformat(timespec="seconds")),
    ).fetchall()
    results = _expire_send_yourself(service, now)
    for message in (OutboxMessage.from_row(row) for row in rows):
        try:
            if not _retry_wait_over(service.db, message, now):
                continue
            result = _deliver_one(service, senders, message, now)
        except Exception as error:
            # Something unexpected with this one message (e.g. the database was busy too long).
            # Its transaction was rolled back, so it stays pending and is tried again next run;
            # meanwhile the remaining due messages are still processed.
            result = Delivery(message, f"message #{message.id}", "error", f"{type(error).__name__}: {error}")
        if result:
            results.append(result)
    return results


def _expire_send_yourself(service, now: datetime) -> list[Delivery]:
    """"Send yourself" messages are never sent automatically - but once they are too
    late (same rules as automatic messages), they are marked cancelled."""
    rows = service.db.execute(
        "SELECT * FROM outbox WHERE status = ? AND send_after <= ? ORDER BY send_after, id",
        (OutboxStatus.SEND_YOURSELF.value, now.isoformat(timespec="seconds")),
    ).fetchall()
    results = []
    for message in (OutboxMessage.from_row(row) for row in rows):
        try:
            with service.write_transaction():
                booking = service.get_booking_by_id(message.booking_id)
                reason = _no_longer_valid(service.info, booking, message, now)
                if not reason:
                    continue
                expired = service.db.execute(
                    "UPDATE outbox SET status = ?, last_error = ?, updated_at = ? WHERE id = ? AND status = ?",
                    (OutboxStatus.CANCELLED.value, reason, now.isoformat(timespec="seconds"), message.id,
                     OutboxStatus.SEND_YOURSELF.value),
                ).rowcount
                if expired:
                    service.add_system_history(booking.id, "outbox",
                                               f"{message.kind.value} to send yourself expired: {reason}")
                    results.append(Delivery(message, booking.reference, "cancelled",
                                            f"send yourself - expired: {reason}"))
        except Exception as error:
            results.append(Delivery(message, f"message #{message.id}", "error", f"{type(error).__name__}: {error}"))
    return results


def _retry_wait_over(db: sqlite3.Connection, message: OutboxMessage, now: datetime) -> bool:
    if message.attempts == 0:
        return True
    last_try = datetime.fromisoformat(
        db.execute("SELECT updated_at FROM outbox WHERE id = ?", (message.id,)).fetchone()[0]
    )
    return now >= last_try + RETRY_WAITS[min(message.attempts, len(RETRY_WAITS)) - 1]


def _deliver_one(service, senders: dict, message: OutboxMessage, now: datetime) -> Delivery | None:
    db, stamp = service.db, now.isoformat(timespec="seconds")

    # 1. Claim and re-check, in one transaction.
    with service.write_transaction():
        booking = service.get_booking_by_id(message.booking_id)
        reason = _no_longer_valid(service.info, booking, message, now)
        if reason:
            db.execute("UPDATE outbox SET status = ?, last_error = ?, updated_at = ? WHERE id = ? AND status = ?",
                       (OutboxStatus.CANCELLED.value, reason, stamp, message.id, OutboxStatus.PENDING.value))
            service.add_system_history(booking.id, "outbox", f"{message.kind.value} not sent: {reason}")
            return Delivery(message, booking.reference, "cancelled", reason)
        claimed = db.execute(
            "UPDATE outbox SET attempts = attempts + 1, updated_at = ? "
            "WHERE id = ? AND status = ? AND attempts = ?",
            (stamp, message.id, OutboxStatus.PENDING.value, message.attempts),
        ).rowcount
    if not claimed:
        return None   # another sender process got there first

    # 2. Send, outside the database.
    attempt = message.attempts + 1
    error = _send(senders, message)

    # 3. Record the result, in one transaction - but only if the message is still
    #    pending. If it was cancelled while send() was running (e.g. the owner
    #    cancelled the booking), the cancellation stands: no status change, no handoff.
    kind, where = message.kind.value, f"via {message.channel} to {message.recipient}"
    with service.write_transaction():
        still_pending = db.execute("SELECT status FROM outbox WHERE id = ?", (message.id,)).fetchone()[0] \
            == OutboxStatus.PENDING.value
        if not still_pending:
            what = "was delivered" if error is None else f"could not be delivered ({error})"
            service.add_system_history(booking.id, "outbox",
                                       f"{kind} {what}, but it had been cancelled while sending")
            return Delivery(message, booking.reference, "cancelled",
                            f"cancelled while sending - the {kind} {what}")

        if error is None:
            db.execute("UPDATE outbox SET status = ?, sent_at = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                       (OutboxStatus.SENT.value, stamp, stamp, message.id))
            service.add_system_history(booking.id, "outbox", f"{kind} sent {where}")
            return Delivery(message, booking.reference, "sent", where)

        if error.permanent or attempt >= MAX_ATTEMPTS:
            db.execute("UPDATE outbox SET status = ?, last_error = ?, updated_at = ? WHERE id = ?",
                       (OutboxStatus.FAILED.value, str(error), stamp, message.id))
            attempts_text = "1 attempt" if attempt == 1 else f"{attempt} attempts"
            handoff_id = service.add_system_handoff(
                booking,
                f"Could not send the {kind} for {booking.reference} to {message.recipient} "
                f"via {message.channel} after {attempts_text} (last error: {error}). "
                "Please contact the customer yourself.",
            )
            service.add_system_history(booking.id, "outbox",
                                       f"{kind} failed after {attempts_text}: {error}; handoff #{handoff_id}")
            detail = f"{error} - handoff #{handoff_id} created"
            if message.kind is OutboxKind.CONFIRMATION:
                # Never remind a customer about a booking they were never told was confirmed.
                if cancel_waiting(db, booking.id, now, kinds=(OutboxKind.REMINDER,)):
                    service.add_system_history(booking.id, "outbox",
                                               "reminder cancelled: the confirmation was never delivered")
                    detail += "; its reminder was cancelled"
            return Delivery(message, booking.reference, "failed", detail)

        wait = int(RETRY_WAITS[attempt - 1].total_seconds() // 60)
        db.execute("UPDATE outbox SET last_error = ?, updated_at = ? WHERE id = ?", (str(error), stamp, message.id))
        service.add_system_history(booking.id, "outbox",
                                   f"{kind} attempt {attempt} failed: {error}; next try in {wait} min")
        return Delivery(message, booking.reference, "retry", f"{error} - next try in {wait} min")


def _no_longer_valid(info: BusinessInfo, booking, message: OutboxMessage, now: datetime) -> str | None:
    """Why this message must not be sent any more - or None if it's still right to send."""
    if booking.status is not BookingStatus.CONFIRMED:
        return f"the booking is {booking.status.value}"
    if message.kind is OutboxKind.REMINDER:
        if reminder_time(info, booking) != message.send_after:
            return "the booking was moved"
        start = datetime.combine(booking.booking_date, info.pricing.start_time, tzinfo=info.timezone)
        if now >= start:
            return "too late: the setup has already started"
    elif booking.booking_date < now.date():
        return "too late: the booking date has passed"
    return None


def _send(senders: dict, message: OutboxMessage):
    """Returns None on success, or a SendError describing what went wrong."""
    sender = senders.get(message.channel)
    if sender is None:
        return SendError(f"no sender is set up for the {message.channel!r} channel", permanent=True)
    try:
        sender.send(message.recipient, message.text)
    except SendError as error:
        return error
    except Exception as error:   # a bug in one sender must not stop the others; treat it as temporary
        return SendError(f"{type(error).__name__}: {error}")
    return None
