"""Tests for the database tables and their safety nets."""

import sqlite3
from pathlib import Path

import pytest

from cozysetup import database
from cozysetup.database import (
    SCHEMA_V1,
    SCHEMA_VERSION,
    Language,
    OutboxKind,
    OutboxStatus,
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
    assert tables == {"bookings", "booking_events", "handoffs", "outbox"}


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



# --- Table version 2: upgrading an existing database (Piece 6.1) ----------------------

def make_version_1_file(path):
    """A database exactly as the version 1 code created it, with some real data in it."""
    connection = sqlite3.connect(path)
    connection.executescript(f"BEGIN; {SCHEMA_V1} PRAGMA user_version = 1; COMMIT;")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    booking_id = add_booking(connection, status="confirmed")
    with connection:
        connection.execute("INSERT INTO booking_events (booking_id, happened_at, actor, event) VALUES (?, ?, 'owner', 'x')",
                           (booking_id, NOW))
        connection.execute("INSERT INTO handoffs (created_at, type, summary) VALUES (?, 'weather', 'windy?')", (NOW,))
    connection.close()


def columns(connection, table):
    return [row["name"] for row in connection.execute(f"PRAGMA table_info({table})")]


def test_a_new_database_starts_at_version_2(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
    assert "language" in columns(db, "bookings")


def test_a_version_1_file_is_upgraded_and_keeps_all_its_data(tmp_path):
    path = tmp_path / "cozysetup.db"
    make_version_1_file(path)

    connection = connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    booking = connection.execute("SELECT * FROM bookings").fetchone()
    assert (booking["reference"], booking["status"], booking["rental_price"]) == ("CS-0001", "confirmed", 50_000)
    assert booking["language"] is None          # unknown for bookings made before version 2
    assert connection.execute("SELECT COUNT(*) FROM booking_events").fetchone()[0] == 1
    assert connection.execute("SELECT summary FROM handoffs").fetchone()[0] == "windy?"
    assert connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0
    connection.close()


def test_a_copy_is_saved_before_upgrading(tmp_path):
    path = tmp_path / "cozysetup.db"
    make_version_1_file(path)
    connect(path).close()

    backup = tmp_path / "cozysetup.db.before-v2.bak"
    assert backup.exists()
    old = sqlite3.connect(backup)
    assert old.execute("PRAGMA user_version").fetchone()[0] == 1          # the untouched original
    assert old.execute("SELECT reference FROM bookings").fetchone()[0] == "CS-0001"
    old.close()


def test_opening_an_upgraded_database_again_changes_nothing(tmp_path):
    path = tmp_path / "cozysetup.db"
    make_version_1_file(path)
    connect(path).close()
    backup = tmp_path / "cozysetup.db.before-v2.bak"
    first_backup = backup.read_bytes()

    connection = connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
    connection.close()
    assert backup.read_bytes() == first_backup     # not overwritten


def test_a_new_file_needs_no_backup(tmp_path):
    connect(tmp_path / "new.db").close()
    assert list(tmp_path.glob("*.bak")) == []


def test_a_failed_upgrade_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "cozysetup.db"
    make_version_1_file(path)
    monkeypatch.setitem(database.MIGRATIONS, 2, "CREATE TABLE outbox (id INTEGER); THIS IS NOT SQL;")

    with pytest.raises(sqlite3.Error):
        connect(path)

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "outbox" not in tables                    # the half-done step was rolled back
    assert connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
    connection.close()


def test_a_database_from_newer_code_is_refused(tmp_path):
    path = tmp_path / "cozysetup.db"
    connection = connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()
    with pytest.raises(DatabaseError, match="made by newer code"):
        connect(path)


def test_new_and_upgraded_databases_have_identical_tables(tmp_path):
    make_version_1_file(tmp_path / "old.db")
    upgraded, new = connect(tmp_path / "old.db"), connect(tmp_path / "new.db")
    schema = "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    assert [tuple(r) for r in upgraded.execute(schema)] == [tuple(r) for r in new.execute(schema)]
    upgraded.close()
    new.close()


# --- The customer's language on a booking ------------------------------------------------

@pytest.mark.parametrize("language", [*list(Language), None])
def test_booking_language_accepts_en_ar_arabizi_or_unknown(db, language):
    add_booking(db, language=language)


def test_booking_language_refuses_anything_else(db):
    with pytest.raises(sqlite3.IntegrityError):
        add_booking(db, language="french")


# --- The outbox table --------------------------------------------------------------------

def add_message(db, booking_id, **changes):
    row = {
        "booking_id": booking_id, "kind": "confirmation", "channel": "terminal", "recipient": "customer-1",
        "language": "en", "text": "Your booking CS-0001 is confirmed!", "send_after": NOW,
        "created_at": NOW, "updated_at": NOW,
    }
    row.update(changes)
    names = ", ".join(row)
    values = ", ".join(f":{name}" for name in row)
    with db:
        return db.execute(f"INSERT INTO outbox ({names}) VALUES ({values})", row).lastrowid


def test_a_message_starts_pending_with_no_attempts(db):
    message_id = add_message(db, add_booking(db))
    row = db.execute("SELECT status, attempts, sent_at FROM outbox WHERE id = ?", (message_id,)).fetchone()
    assert tuple(row) == ("pending", 0, None)


@pytest.mark.parametrize("kind", list(OutboxKind))
@pytest.mark.parametrize("status", list(OutboxStatus))
def test_every_kind_and_status_the_code_uses_is_accepted(db, kind, status):
    add_message(db, add_booking(db), kind=kind.value, status=status.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": "birthday_wishes"},
        {"status": "maybe"},
        {"language": "french"},
        {"attempts": -1},
        {"text": None},
        {"send_after": None},
    ],
)
def test_refuses_invalid_message(db, changes):
    booking_id = add_booking(db)
    with pytest.raises(sqlite3.IntegrityError):
        add_message(db, booking_id, **changes)


def test_a_message_must_belong_to_a_real_booking(db):
    with pytest.raises(sqlite3.IntegrityError):
        add_message(db, 999)


def test_never_two_waiting_messages_of_the_same_kind_for_one_booking(db):
    booking_id = add_booking(db)
    add_message(db, booking_id, kind="reminder")
    with pytest.raises(sqlite3.IntegrityError):
        add_message(db, booking_id, kind="reminder")
    with pytest.raises(sqlite3.IntegrityError):
        add_message(db, booking_id, kind="reminder", status="send_yourself")


def test_a_new_message_is_allowed_once_the_old_one_is_done(db):
    booking_id = add_booking(db)
    first = add_message(db, booking_id, kind="reminder")
    with db:
        db.execute("UPDATE outbox SET status = 'cancelled' WHERE id = ?", (first,))
    add_message(db, booking_id, kind="reminder")          # e.g. the booking was moved
    add_message(db, booking_id, kind="confirmation")      # a different kind is fine too
