"""The database: one SQLite file holding bookings, their history, and handoffs.

connect() opens the file (creating it and its tables the first time) and
returns a connection the booking service uses.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "cozysetup.db"

# Bumped whenever the tables change, so an old database file is never used
# with code that expects different tables.
SCHEMA_VERSION = 1


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


# --- The tables -----------------------------------------------------------------
# STRICT: SQLite refuses values of the wrong type (e.g. text in a number column).
# Money is in fils (1 KWD = 1000 fils). Dates are "2026-10-01"; times are
# Kuwait time, e.g. "2026-09-28T14:02:00+03:00".

SCHEMA = """
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


class DatabaseError(Exception):
    """The database file cannot be used by this version of the code."""


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database, creating the file and tables the first time.

    Pass ":memory:" for a temporary database that disappears when closed (tests).
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row             # rows readable by column name
    connection.execute("PRAGMA foreign_keys = ON")   # refuse events for bookings that don't exist
    connection.execute("PRAGMA busy_timeout = 5000") # wait up to 5s if another part is writing

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        try:
            # One transaction: all tables are created, or none.
            connection.executescript(f"BEGIN; {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
        except sqlite3.Error:
            connection.rollback()
            connection.close()
            raise
    elif version != SCHEMA_VERSION:
        connection.close()
        raise DatabaseError(
            f"{path} has table version {version}, but this code expects version {SCHEMA_VERSION}"
        )
    return connection
