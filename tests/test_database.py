"""Tests for the database tables and their safety nets."""

import sqlite3
from pathlib import Path

import pytest

from cozysetup.database import (
    SCHEMA_VERSION,
    Actor,
    BookingStatus,
    DatabaseError,
    HandoffStatus,
    HandoffType,
    connect,
)

NOW = "2026-09-28T14:00:00+03:00"


@pytest.fixture
def db():
    connection = connect(":memory:")
    yield connection
    connection.close()


def add_booking(db, **changes):
    """Insert a valid booking, with any column changed via **changes."""
    row = {
        "reference": "CS-0001", "booking_date": "2026-10-01", "location_id": "julaia",
        "customer_name": "Ahmad", "customer_phone": "+96599999999",
        "channel": "terminal", "channel_user_id": "test",
        "payment_choice": "deposit", "rental_price": 50_000, "amount_now": 25_000,
        "remaining_on_day": 25_000, "security_deposit": 20_000,
        "status": "pending_payment", "created_at": NOW, "updated_at": NOW,
    }
    row.update(changes)
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    with db:
        return db.execute(f"INSERT INTO bookings ({columns}) VALUES ({placeholders})", row).lastrowid


# --- Creating and opening -----------------------------------------------------

def test_creates_the_file_and_tables(tmp_path):
    path = tmp_path / "data" / "test.db"
    connection = connect(path)
    tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    connection.close()
    assert path.exists()
    assert tables == {"bookings", "booking_events", "handoffs"}


def test_opening_again_keeps_the_data(tmp_path):
    path = tmp_path / "test.db"
    connection = connect(path)
    add_booking(connection)
    connection.close()

    connection = connect(path)
    assert connection.execute("SELECT reference FROM bookings").fetchone()["reference"] == "CS-0001"
    connection.close()


def test_refuses_a_database_from_a_different_version(tmp_path):
    path = tmp_path / "test.db"
    connection = connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()
    with pytest.raises(DatabaseError, match="table version"):
        connect(path)


def test_customer_data_is_never_saved_in_git():
    gitignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    assert "data/" in gitignore.splitlines()


# --- The database accepts exactly the values the code uses --------------------

@pytest.mark.parametrize("status", list(BookingStatus))
def test_every_booking_status_is_accepted(db, status):
    add_booking(db, status=status.value)


@pytest.mark.parametrize("handoff_type", list(HandoffType))
def test_every_handoff_type_is_accepted(db, handoff_type):
    with db:
        db.execute("INSERT INTO handoffs (created_at, type, summary) VALUES (?, ?, 'test')",
                   (NOW, handoff_type.value))


def test_every_actor_and_handoff_status_is_accepted(db):
    booking_id = add_booking(db)
    with db:
        for actor in Actor:
            db.execute("INSERT INTO booking_events (booking_id, happened_at, actor, event) VALUES (?, ?, ?, 'test')",
                       (booking_id, NOW, actor.value))
        for status in HandoffStatus:
            db.execute("INSERT INTO handoffs (created_at, type, summary, status) VALUES (?, 'other', 'test', ?)",
                       (NOW, status.value))


# --- Safety nets: the database refuses bad data -------------------------------

@pytest.mark.parametrize(
    "changes",
    [
        {"status": "confirmd"},                      # misspelled status
        {"payment_choice": "half"},                  # unknown payment choice
        {"rental_price": "fifty"},                   # text in a money column (STRICT)
        {"rental_price": 0, "amount_now": 0, "remaining_on_day": 0},
        {"amount_now": 30_000},                      # 30 + 25 does not add up to 50
        {"customer_phone": None},                    # required value missing
        {"date_conflict": 2},
    ],
)
def test_refuses_invalid_booking(db, changes):
    with pytest.raises(sqlite3.IntegrityError):
        add_booking(db, **changes)


def test_references_are_unique(db):
    add_booking(db, reference="CS-0001")
    with pytest.raises(sqlite3.IntegrityError):
        add_booking(db, reference="CS-0001", booking_date="2026-10-02")


def test_many_pending_bookings_may_share_a_date(db):
    add_booking(db, reference="CS-0001", status="pending_payment")
    add_booking(db, reference="CS-0002", status="payment_submitted")
    add_booking(db, reference="CS-0003", status="confirmed")


def test_only_one_confirmed_booking_per_date(db):
    add_booking(db, reference="CS-0001", status="confirmed")
    with pytest.raises(sqlite3.IntegrityError):
        add_booking(db, reference="CS-0002", status="confirmed")


def test_a_cancelled_booking_frees_the_date(db):
    add_booking(db, reference="CS-0001", status="cancelled")
    add_booking(db, reference="CS-0002", status="confirmed")


def test_events_must_belong_to_a_real_booking(db):
    with pytest.raises(sqlite3.IntegrityError):
        with db:
            db.execute("INSERT INTO booking_events (booking_id, happened_at, actor, event) VALUES (999, ?, 'owner', 'x')",
                       (NOW,))
