"""The database: one SQLite file holding bookings, their history, handoffs,
and the outbox of messages waiting to be sent to customers.

connect() opens the file - creating it the first time, and upgrading an older
file to the current table version - and returns a connection.
"""

from __future__ import annotations

import shutil
import sqlite3
from enum import StrEnum
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cozysetup.db"

# The current table version. An older file is upgraded step by step with the
# MIGRATIONS below; a newer one (made by newer code) is refused.
SCHEMA_VERSION = 4


# --- The values the database accepts ------------------------------------------
# These lists must match the CHECK (...) lists in SCHEMA below.

class BookingStatus(StrEnum):
    PENDING_PAYMENT = "pending_payment"      # created; date NOT blocked
    PAYMENT_SUBMITTED = "payment_submitted"  # screenshot received; date NOT blocked
    CONFIRMED = "confirmed"                  # owner verified payment; date blocked
    COMPLETED = "completed"                  # setup done
    CANCELLED = "cancelled"


class Actor(StrEnum):
    """Who made a change."""
    AI = "ai"          # the AI employee, on the customer's behalf
    OWNER = "owner"
    SYSTEM = "system"  # automatic, e.g. flagging a date conflict


class HandoffType(StrEnum):
    SAME_DAY = "same_day"
    CANCELLATION = "cancellation"
    RESCHEDULE = "reschedule"
    WEATHER = "weather"
    DAMAGE = "damage"
    PAYMENT = "payment"
    DATE_CONFLICT = "date_conflict"
    OTHER = "other"


class HandoffStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class Language(StrEnum):
    """The language a customer writes in."""
    ENGLISH = "en"
    ARABIC = "ar"
    ARABIZI = "arabizi"


class OutboxKind(StrEnum):
    CONFIRMATION = "confirmation"
    REMINDER = "reminder"


class OutboxStatus(StrEnum):
    PENDING = "pending"              # waiting for its send time / to be sent
    SEND_YOURSELF = "send_yourself"  # no automatic channel: the owner sends it
    SENT = "sent"
    FAILED = "failed"                # gave up after retries - the owner was told
    CANCELLED = "cancelled"          # no longer needed (booking cancelled or moved)


class InboundStatus(StrEnum):
    WAITING = "waiting"   # recorded by the webhook, not processed yet (or being retried)
    DONE = "done"         # processed: the replies are saved, or nothing needed saying
    FAILED = "failed"     # could not be processed - the owner was told
    IGNORED = "ignored"   # deliberately not answered (too old, or our own echo)


class ReplyKind(StrEnum):
    DISCLOSURE = "disclosure"    # "automated assistant", before the first reply in a conversation
    REPLY = "reply"              # the AI's answer
    UNSUPPORTED = "unsupported"  # voice notes, videos, stickers, reels, shares


class ReplyStatus(StrEnum):
    PENDING = "pending"              # saved, waiting to be sent (or retried)
    SENT = "sent"
    SEND_YOURSELF = "send_yourself"  # Instagram's 24-hour window closed: the owner sends it
    FAILED = "failed"                # gave up - the owner was told
    CANCELLED = "cancelled"          # the owner took over the conversation before it was sent


# --- The tables -----------------------------------------------------------------
# STRICT: SQLite refuses values of the wrong type (e.g. text in a number column).
# Money is in fils (1 KWD = 1000 fils). Dates are "2026-10-01"; times are
# Kuwait time, e.g. "2026-09-28T14:02:00+03:00".
#
# A new database is built exactly like an upgraded one: the version 1 tables
# first, then every migration in order. So new and upgraded files can never differ.

SCHEMA_V1 = """
CREATE TABLE bookings (
    id                INTEGER PRIMARY KEY,
    reference         TEXT    NOT NULL UNIQUE,     -- CS-0001
    booking_date      TEXT    NOT NULL,
    location_id       TEXT    NOT NULL,            -- id from business.toml
    customer_name     TEXT    NOT NULL,
    customer_phone    TEXT    NOT NULL,            -- international format, e.g. +96599999999
    channel           TEXT    NOT NULL,            -- where the customer wrote from
    channel_user_id   TEXT    NOT NULL,            -- who they are on that channel
    payment_choice    TEXT    NOT NULL CHECK (payment_choice IN ('deposit', 'full')),
    rental_price      INTEGER NOT NULL CHECK (rental_price > 0),
    amount_now        INTEGER NOT NULL CHECK (amount_now > 0),
    remaining_on_day  INTEGER NOT NULL CHECK (remaining_on_day >= 0),
    security_deposit  INTEGER NOT NULL CHECK (security_deposit >= 0),
    status            TEXT    NOT NULL CHECK (status IN
                          ('pending_payment', 'payment_submitted', 'confirmed', 'completed', 'cancelled')),
    date_conflict     INTEGER NOT NULL DEFAULT 0 CHECK (date_conflict IN (0, 1)),
    payment_proof     TEXT,                        -- path of the screenshot, if sent
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL,
    CHECK (amount_now + remaining_on_day = rental_price)
) STRICT;

-- Safety net: the database itself refuses a second confirmed booking for the
-- same date, even if the code had a bug. Pending bookings are not limited.
CREATE UNIQUE INDEX one_confirmed_booking_per_date
    ON bookings (booking_date) WHERE status IN ('confirmed', 'completed');

-- History of every change to a booking. Rows are only ever added.
CREATE TABLE booking_events (
    id           INTEGER PRIMARY KEY,
    booking_id   INTEGER NOT NULL REFERENCES bookings (id),
    happened_at  TEXT    NOT NULL,
    actor        TEXT    NOT NULL CHECK (actor IN ('ai', 'owner', 'system')),
    event        TEXT    NOT NULL,                 -- e.g. 'created', 'status_changed'
    old_status   TEXT,
    new_status   TEXT,
    details      TEXT    NOT NULL DEFAULT ''
) STRICT;

-- The owner's to-do list: everything the AI handed over.
CREATE TABLE handoffs (
    id               INTEGER PRIMARY KEY,
    created_at       TEXT    NOT NULL,
    type             TEXT    NOT NULL CHECK (type IN
                         ('same_day', 'cancellation', 'reschedule', 'weather',
                          'damage', 'payment', 'date_conflict', 'other')),
    summary          TEXT    NOT NULL,
    customer_name    TEXT,
    customer_phone   TEXT,
    channel          TEXT,
    channel_user_id  TEXT,
    booking_id       INTEGER REFERENCES bookings (id),
    status           TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    resolved_at      TEXT,
    resolution_note  TEXT
) STRICT;
"""

# Each migration takes the database from the previous version to this one.
MIGRATIONS = {
    2: """
-- The customer's language, so messages can later use the owner's approved
-- Arabic wording. Empty (NULL) for bookings made before it was recorded.
ALTER TABLE bookings ADD COLUMN language TEXT CHECK (language IN ('en', 'ar', 'arabizi'));

-- Messages the system sends to customers by itself (not replies in a chat).
CREATE TABLE outbox (
    id           INTEGER PRIMARY KEY,
    booking_id   INTEGER NOT NULL REFERENCES bookings (id),
    kind         TEXT    NOT NULL CHECK (kind IN ('confirmation', 'reminder')),
    channel      TEXT    NOT NULL,                 -- where to send it, e.g. terminal, owner
    recipient    TEXT    NOT NULL,                 -- who on that channel
    language     TEXT    NOT NULL CHECK (language IN ('en', 'ar', 'arabizi')),
    text         TEXT    NOT NULL,                 -- the exact message, filled in when queued
    send_after   TEXT    NOT NULL,                 -- Kuwait time; not sent before this
    status       TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN
                     ('pending', 'send_yourself', 'sent', 'failed', 'cancelled')),
    attempts     INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error   TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    sent_at      TEXT
) STRICT;

-- Safety net: never two waiting messages of the same kind for one booking
-- (e.g. two reminders). Sent and cancelled ones don't count.
CREATE UNIQUE INDEX one_waiting_message_per_kind
    ON outbox (booking_id, kind) WHERE status IN ('pending', 'send_yourself');

-- Finding the messages that are due, quickly.
CREATE INDEX outbox_due ON outbox (status, send_after);
""",
    3: """
-- One conversation per customer on a real channel (Instagram). Instagram DMs
-- arrive one by one to a server that can restart, so everything the AI needs
-- to continue the conversation is kept here.
CREATE TABLE conversations (
    id                        INTEGER PRIMARY KEY,
    channel                   TEXT    NOT NULL,          -- e.g. instagram
    channel_user_id           TEXT    NOT NULL,          -- the customer's id on that channel (Instagram: IGSID)
    language                  TEXT    CHECK (language IN ('en', 'ar', 'arabizi')),
    history                   TEXT    NOT NULL DEFAULT '[]',   -- JSON: the AI conversation, replayed every time
    last_preview              TEXT,                      -- JSON: booking details last shown as a summary
    disclosure_sent           INTEGER NOT NULL DEFAULT 0 CHECK (disclosure_sent IN (0, 1)),
    ai_paused                 INTEGER NOT NULL DEFAULT 0 CHECK (ai_paused IN (0, 1)),
    paused_at                 TEXT,
    pause_reason              TEXT,
    last_customer_message_at  TEXT,                      -- for the 24-hour messaging window
    created_at                TEXT    NOT NULL,
    updated_at                TEXT    NOT NULL,
    UNIQUE (channel, channel_user_id)
) STRICT;

-- Images a customer sent in a conversation (the files are kept next to the database).
CREATE TABLE conversation_attachments (
    id               INTEGER PRIMARY KEY,
    conversation_id  INTEGER NOT NULL REFERENCES conversations (id),
    number           INTEGER NOT NULL CHECK (number >= 1),   -- "[Customer attached image #N]"
    file_name        TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    UNIQUE (conversation_id, number)
) STRICT;

-- Every message received from a channel, recorded once (the channel's message id
-- is unique), then processed in order. Filled from Step 7.3 (webhooks).
CREATE TABLE inbound_messages (
    id               INTEGER PRIMARY KEY,
    channel          TEXT    NOT NULL,
    external_id      TEXT    NOT NULL,                   -- Instagram: the message id (mid)
    channel_user_id  TEXT    NOT NULL,
    received_at      TEXT    NOT NULL,
    payload          TEXT    NOT NULL,                   -- the event exactly as received (JSON)
    status           TEXT    NOT NULL DEFAULT 'waiting' CHECK (status IN ('waiting', 'done', 'failed', 'ignored')),
    processed_at     TEXT,
    error            TEXT,
    UNIQUE (channel, external_id)
) STRICT;

CREATE INDEX inbound_waiting ON inbound_messages (status, received_at);
""",
    4: """
-- Processing an inbound message can fail for a moment (e.g. an image download):
-- it is tried again later, a limited number of times.
ALTER TABLE inbound_messages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0);
ALTER TABLE inbound_messages ADD COLUMN last_attempt_at TEXT;

-- The replies to send in a conversation (Instagram), saved together with the
-- conversation before they are sent, then sent in order. One row per Instagram
-- message: a long reply is split into several rows.
CREATE TABLE conversation_replies (
    id               INTEGER PRIMARY KEY,
    conversation_id  INTEGER NOT NULL REFERENCES conversations (id),
    kind             TEXT    NOT NULL CHECK (kind IN ('disclosure', 'reply', 'unsupported')),
    text             TEXT    NOT NULL,
    status           TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN
                         ('pending', 'sent', 'send_yourself', 'failed', 'cancelled')),
    attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error       TEXT,
    external_id      TEXT,                          -- Instagram's message id once sent: recognises its echo
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    sent_at          TEXT
) STRICT;

CREATE INDEX conversation_replies_pending ON conversation_replies (status, id);
CREATE UNIQUE INDEX conversation_replies_sent_id ON conversation_replies (external_id) WHERE external_id IS NOT NULL;
""",
}


class DatabaseError(Exception):
    """The database file cannot be used by this version of the code."""


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database: create it the first time, upgrade an older one.

    Before upgrading a database file, a copy is saved next to it
    (e.g. cozysetup.db.before-v2.bak). Pass ":memory:" for a temporary
    database that disappears when closed (tests).
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row             # rows readable by column name
    connection.execute("PRAGMA foreign_keys = ON")   # refuse rows pointing at bookings that don't exist
    connection.execute("PRAGMA busy_timeout = 5000") # wait up to 5s if another part is writing

    try:
        version = _version(connection)
        if version > SCHEMA_VERSION:
            raise DatabaseError(
                f"{path} has table version {version}, but this code only knows up to "
                f"version {SCHEMA_VERSION} - it was made by newer code"
            )
        if version == 0:
            _run(connection, SCHEMA_V1, new_version=1)
            version = 1
        elif version < SCHEMA_VERSION and path != ":memory:":
            _back_up(connection, Path(path))
        while version < SCHEMA_VERSION:
            version += 1
            _run(connection, MIGRATIONS[version], new_version=version)
    except BaseException:
        connection.close()
        raise
    return connection


def _version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def _run(connection: sqlite3.Connection, sql: str, *, new_version: int) -> None:
    """One step in one transaction: all its changes and the new version number, or nothing."""
    try:
        connection.executescript(f"BEGIN; {sql} PRAGMA user_version = {new_version}; COMMIT;")
    except sqlite3.Error:
        connection.rollback()
        raise


def _back_up(connection: sqlite3.Connection, path: Path) -> Path:
    """Copy the file before upgrading it, so nothing is lost if anything goes wrong."""
    backup = path.with_name(f"{path.name}.before-v{SCHEMA_VERSION}.bak")
    if not backup.exists():
        connection.execute("PRAGMA wal_checkpoint")   # make sure the file on disk is complete
        shutil.copy2(path, backup)
    return backup
