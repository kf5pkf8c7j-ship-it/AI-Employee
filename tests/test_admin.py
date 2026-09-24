"""Tests for the owner's commands."""

import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

from cozysetup import admin
from cozysetup.bookings import BookingService
from cozysetup.business_info import load_business_info
from cozysetup.database import connect

INFO = load_business_info()
# Pretend it is Monday 28 September 2026, 2 PM in Kuwait.
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=INFO.timezone)
TUESDAY = date(2026, 9, 29)
THURSDAY = date(2026, 10, 1)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "practice.db"


@pytest.fixture
def service(db_path):
    db = connect(db_path)
    yield BookingService(db, INFO, clock=lambda: NOW, proofs_dir=db_path.parent / "payment_proofs")
    db.close()


def run(db_path, *argv):
    return admin.main(["--db", str(db_path), *argv], clock=lambda: NOW)


def book(service, name, phone, booking_date=THURSDAY, choice="deposit"):
    return service.create_booking(
        booking_date=booking_date, location_id="julaia", customer_name=name, customer_phone=phone,
        payment_choice=choice, channel="terminal", channel_user_id=name,
    )


# --- bookings ---------------------------------------------------------------------

def test_bookings_when_there_are_none(db_path, capsys):
    assert run(db_path, "bookings") == 0
    assert "No upcoming bookings." in capsys.readouterr().out


def test_bookings_shows_readable_dates_names_and_amounts(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    book(service, "Fahad", "55555555", booking_date=TUESDAY, choice="full")
    assert run(db_path, "bookings") == 0
    out = capsys.readouterr().out
    assert out.index("CS-0002") < out.index("CS-0001")          # date order
    assert "Tue 29 Sep 2026 (tomorrow)" in out
    assert "Thu 1 Oct 2026" in out
    assert "Julaia" in out and "julaia" not in out               # name, not id
    assert "deposit 25 KWD" in out and "full 50 KWD" in out      # KWD, not fils
    assert "waiting for payment" in out


def test_bookings_filters(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    book(service, "Fahad", "55555555", booking_date=TUESDAY, choice="full")
    service.approve_payment("CS-0001")
    run(db_path, "bookings", "--status", "confirmed")
    out = capsys.readouterr().out
    assert "CS-0001" in out and "CS-0002" not in out
    run(db_path, "bookings", "--date", "2026-09-29")
    out = capsys.readouterr().out
    assert "CS-0002" in out and "CS-0001" not in out


def test_past_bookings_only_with_all(service, db_path, capsys):
    book(service, "Ahmad", "99999999", booking_date=TUESDAY, choice="full")
    wednesday = lambda: datetime(2026, 9, 30, 12, 0, tzinfo=INFO.timezone)
    admin.main(["--db", str(db_path), "bookings"], clock=wednesday)
    assert "No upcoming bookings." in capsys.readouterr().out
    admin.main(["--db", str(db_path), "bookings", "--all"], clock=wednesday)
    assert "CS-0001" in capsys.readouterr().out


def test_invalid_date_is_explained(db_path, capsys):
    with pytest.raises(SystemExit) as exit_:
        run(db_path, "bookings", "--date", "1-10-2026")
    assert exit_.value.code == 2
    assert "write it like 2026-10-01" in capsys.readouterr().err


# --- pending ----------------------------------------------------------------------

def test_pending_lists_only_waiting_bookings_and_flags_conflicts(service, db_path, capsys):
    book(service, "Ahmad", "99999999")                             # CS-0001
    book(service, "Sara", "55555555")                              # CS-0002, same date
    service.attach_payment_proof("CS-0002", "55555555", b"\xff\xd8\xff" + bytes(50))
    service.approve_payment("CS-0001")
    assert run(db_path, "pending") == 0
    out = capsys.readouterr().out
    assert "1 booking(s) waiting" in out
    assert "CS-0001" not in out
    assert "CS-0002" in out
    assert "screenshot received - check your account" in out
    assert "DATE CONFLICT" in out


def test_pending_when_nothing_waits(db_path, capsys):
    run(db_path, "pending")
    assert "Nothing is waiting" in capsys.readouterr().out


# --- show -------------------------------------------------------------------------

def test_show_gives_the_full_picture(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001", "25 KWD received")
    assert run(db_path, "show", "cs-0001") == 0
    out = capsys.readouterr().out
    assert "CS-0001  -  confirmed" in out
    assert "Thu 1 Oct 2026, 6 PM – 11 PM" in out
    assert "Ahmad  +96599999999" in out
    assert "deposit 25 KWD" in out
    assert "25 KWD remaining + 20 KWD security deposit" in out
    assert "owner   status_changed" in out
    assert "(25 KWD received)" in out


def test_show_unknown_booking(db_path, capsys):
    assert run(db_path, "show", "CS-0099") == 1
    assert "No booking CS-0099" in capsys.readouterr().err


# --- The installed command ----------------------------------------------------------

def test_the_command_is_installed():
    command = Path(sys.executable).parent / "cozysetup-admin"
    result = subprocess.run([command, "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "the owner's commands" in result.stdout


def test_db_option_uses_the_given_practice_file(db_path):
    assert not db_path.exists()
    run(db_path, "bookings")
    assert db_path.exists()


# --- Acting commands (Piece 3.3) --------------------------------------------------------

def answer(reply):
    """Pretend the owner types `reply` at the confirmation question."""
    questions = []

    def ask(question):
        questions.append(question)
        return reply
    ask.questions = questions
    return ask


def act(db_path, reply, *argv):
    ask = answer(reply)
    code = admin.main(["--db", str(db_path), *argv], clock=lambda: NOW, ask=ask)
    return code, ask.questions


ADD = ["add", "--date", "2026-10-01", "--location", "Arifjan", "--name", "Sara",
       "--phone", "+966 50 123 4567", "--payment", "deposit"]


def test_add_asks_first_and_creates_a_waiting_booking(service, db_path, capsys):
    code, questions = act(db_path, "y", *ADD)
    assert code == 0
    assert questions == ["Confirm? [y/N] "]
    out = capsys.readouterr().out
    assert "Sara (+966 50 123 4567) - Thu 1 Oct 2026, Julaia" in out   # Arifjan -> Julaia
    assert "✓ CS-0001 created - waiting for payment" in out
    booking = service.get_booking("CS-0001")
    assert (booking.location_id, booking.customer_phone) == ("julaia", "+966501234567")


@pytest.mark.parametrize("reply", ["", "n", "no", "nope"])
def test_anything_but_yes_changes_nothing(service, db_path, capsys, reply):
    code, _ = act(db_path, reply, *ADD)
    assert code == 1
    assert "Nothing changed." in capsys.readouterr().out
    assert service.list_bookings() == []


def test_no_answer_at_all_changes_nothing(service, db_path, capsys):
    def no_keyboard(question):
        raise EOFError
    assert admin.main(["--db", str(db_path), *ADD], clock=lambda: NOW, ask=no_keyboard) == 1
    assert service.list_bookings() == []


def test_yes_flag_skips_the_question(service, db_path):
    code, questions = act(db_path, "n", *ADD, "--yes")
    assert (code, questions) == (0, [])
    assert service.get_booking("CS-0001") is not None


def test_add_paid_confirms_now_and_warns_about_waiting_bookings(service, db_path, capsys):
    book(service, "Waiting", "55555555")                    # CS-0001, Thursday
    code, _ = act(db_path, "y", "add", "--date", "2026-10-01", "--location", "julaia", "--name", "Ahmad",
                  "--phone", "99999999", "--payment", "full", "--paid")
    out = capsys.readouterr().out
    assert code == 0
    assert "It will be CONFIRMED now" in out
    assert "1 other booking(s) waiting for this date will be flagged" in out   # warned before
    assert "✓ CS-0002 created - confirmed" in out
    assert "CS-0001 (Waiting) was waiting for the same date - flagged" in out  # reported after
    assert service.get_booking("CS-0001").date_conflict is True


def test_add_today_by_word(service, db_path):
    act(db_path, "y", "add", "--date", "today", "--location", "julaia", "--name", "Mona",
        "--phone", "66666666", "--payment", "full", "--paid")
    assert service.get_booking("CS-0001").booking_date == NOW.date()


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"--date": "today", "--payment": "deposit"}, "the payment must be: full"),
        ({"--date": "2026-09-27"}, "has passed"),
        ({"--location": "kabd"}, "Unknown location 'kabd'"),
    ],
)
def test_add_refuses_before_asking(service, db_path, capsys, changes, message):
    argv = list(ADD)
    for option, value in changes.items():
        argv[argv.index(option) + 1] = value
    code, questions = act(db_path, "y", *argv)
    assert (code, questions) == (1, [])
    assert message in capsys.readouterr().err
    assert service.list_bookings() == []


def test_add_on_a_confirmed_date_is_refused_before_asking(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    code, questions = act(db_path, "y", *ADD)
    assert (code, questions) == (1, [])
    assert "already has a confirmed booking" in capsys.readouterr().err


def test_add_with_a_bad_phone_is_refused_by_the_service(service, db_path, capsys):
    argv = list(ADD)
    argv[argv.index("--phone") + 1] = "123"
    assert act(db_path, "y", *argv)[0] == 1
    assert "not a valid phone number" in capsys.readouterr().err


def test_approve(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    book(service, "Sara", "55555555")
    code, _ = act(db_path, "y", "approve", "cs-0001", "25 KWD received")
    out = capsys.readouterr().out
    assert code == 0
    assert "You confirm 25 KWD arrived in your account" in out
    assert "No screenshot was sent" in out
    assert "✓ CS-0001 confirmed" in out
    assert "CS-0002 (Sara) was waiting for the same date - flagged" in out
    assert "has not been messaged" in out
    approval = [e for e in service.booking_history("CS-0001") if e["new_status"] == "confirmed"][-1]
    assert approval["details"] == "25 KWD received"


def test_approve_refused_before_asking_when_date_is_taken(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    book(service, "Sara", "55555555")
    service.approve_payment("CS-0001")
    code, questions = act(db_path, "y", "approve", "CS-0002")
    assert (code, questions) == (1, [])
    assert "already has a confirmed booking" in capsys.readouterr().err


def test_reject(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.attach_payment_proof("CS-0001", "99999999", b"\xff\xd8\xff" + bytes(50))
    assert act(db_path, "y", "reject", "CS-0001", "no transfer found")[0] == 0
    assert service.get_booking("CS-0001").status.value == "pending_payment"


def test_reject_without_screenshot_refused_before_asking(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    assert act(db_path, "y", "reject", "CS-0001") == (1, [])
    assert "there is no screenshot to reject" in capsys.readouterr().err


def test_cancel_confirmed_booking_mentions_refund_and_waiting_bookings(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    book(service, "Sara", "55555555")
    service.approve_payment("CS-0001")
    assert act(db_path, "y", "cancel", "CS-0001", "customer cancelled in time")[0] == 0
    out = capsys.readouterr().out
    assert "They paid 25 KWD. Any refund is up to you" in out
    assert "is free again" in out
    assert "Still waiting for that date (you may approve one): CS-0002" in out


def test_reschedule(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    book(service, "Sara", "55555555", booking_date=date(2026, 10, 8))
    assert act(db_path, "y", "reschedule", "CS-0001", "2026-10-08", "bad weather")[0] == 0
    out = capsys.readouterr().out
    assert "Move CS-0001 from Thu 1 Oct 2026 to Thu 8 Oct 2026?" in out
    assert "CS-0002 (Sara) was waiting for the same date - flagged" in out
    assert service.get_booking("CS-0001").booking_date == date(2026, 10, 8)


@pytest.mark.parametrize(("new_date", "message"), [("2026-10-01", "is already on"), ("2026-09-27", "has passed")])
def test_reschedule_refused_before_asking(service, db_path, capsys, new_date, message):
    book(service, "Ahmad", "99999999")
    assert act(db_path, "y", "reschedule", "CS-0001", new_date) == (1, [])
    assert message in capsys.readouterr().err


def test_complete_refused_before_the_day_and_allowed_on_it(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    assert act(db_path, "y", "complete", "CS-0001") == (1, [])
    assert "is not until Thu 1 Oct 2026" in capsys.readouterr().err

    evening = lambda: datetime(2026, 10, 1, 23, 0, tzinfo=INFO.timezone)
    assert admin.main(["--db", str(db_path), "complete", "CS-0001", "-y"], clock=evening) == 0
    assert service.get_booking("CS-0001").status.value == "completed"


@pytest.mark.parametrize("command", ["approve", "reject", "cancel", "complete"])
def test_unknown_reference(db_path, capsys, command):
    assert act(db_path, "y", command, "CS-0404") == (1, [])
    assert "No booking CS-0404" in capsys.readouterr().err


# --- Handoffs, screenshots and the overview (Piece 3.4) ------------------------------------

def test_handoffs_lists_open_ones_with_customer_and_booking(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    book(service, "Sara", "55555555")
    service.approve_payment("CS-0001")
    service.create_handoff("weather", "Asks if Saturday will be windy", customer_name="Khalid",
                           customer_phone="90000000")
    assert run(db_path, "handoffs") == 0
    out = capsys.readouterr().out
    assert "#1  [date conflict]" in out
    assert "Customer: CS-0002 - Sara +96555555555" in out
    assert "#2  [weather]" in out
    assert "Customer: Khalid +96590000000" in out
    assert "Asks if Saturday will be windy" in out


def test_no_open_handoffs(db_path, capsys):
    run(db_path, "handoffs")
    assert "No open handoffs" in capsys.readouterr().out


def test_resolve_asks_then_moves_it_out_of_the_open_list(service, db_path, capsys):
    service.create_handoff("weather", "Asks about wind")
    assert act(db_path, "y", "resolve", "1", "Called them, all fine")[0] == 0
    assert "✓ Handoff #1 resolved." in capsys.readouterr().out
    run(db_path, "handoffs")
    assert "No open handoffs" in capsys.readouterr().out
    run(db_path, "handoffs", "--all")
    assert "✓ Resolved" in capsys.readouterr().out


def test_resolve_said_no_keeps_it_open(service, db_path):
    service.create_handoff("weather", "Asks about wind")
    assert act(db_path, "", "resolve", "1")[0] == 1
    assert len(service.list_handoffs()) == 1


@pytest.mark.parametrize("number", ["1", "99"])
def test_resolve_refused_before_asking(service, db_path, capsys, number):
    handoff = service.create_handoff("weather", "x")
    service.resolve_handoff(handoff.id)
    assert act(db_path, "y", "resolve", number) == (1, [])
    assert ("already resolved" if number == "1" else "No handoff #99") in capsys.readouterr().err


def open_recorder():
    opened = []
    return opened, opened.append


def test_proof_opens_the_current_screenshot(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.attach_payment_proof("CS-0001", "99999999", b"\xff\xd8\xff" + bytes(50))
    service.attach_payment_proof("CS-0001", "99999999", b"\x89PNG\r\n\x1a\n" + bytes(50))
    opened, opener = open_recorder()
    assert admin.main(["--db", str(db_path), "proof", "CS-0001"], clock=lambda: NOW, open_file=opener) == 0
    assert [path.name for path in opened] == ["CS-0001-2.png"]
    out = capsys.readouterr().out
    assert "a screenshot is not proof, check your account" in out
    assert "Earlier screenshots for this booking: CS-0001-1.jpg" in out


def test_proof_when_there_is_none(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    opened, opener = open_recorder()
    admin.main(["--db", str(db_path), "proof", "CS-0001"], clock=lambda: NOW, open_file=opener)
    assert opened == []
    assert "has no payment screenshot" in capsys.readouterr().out


def test_overview_when_all_is_clear(db_path, capsys):
    assert run(db_path, "overview") == 0
    out = capsys.readouterr().out
    assert "CozySetup overview - Mon 28 Sep 2026 (today)" in out
    assert "Nothing - all clear." in out
    assert "No setup today." in out


def test_overview_shows_what_needs_attention(service, db_path, capsys):
    service.create_owner_booking(booking_date=NOW.date(), location_id="al_subiya", customer_name="Mona",
                                 customer_phone="66666666", payment_choice="full", paid=True)   # CS-0001 today
    book(service, "Ahmad", "99999999")                                                          # CS-0002 Thu
    book(service, "Sara", "55555555")                                                           # CS-0003 Thu
    service.attach_payment_proof("CS-0003", "55555555", b"\xff\xd8\xff" + bytes(50))
    service.approve_payment("CS-0002")
    book(service, "Fahad", "50000000", booking_date=date(2026, 10, 20))                         # CS-0004 waiting

    run(db_path, "overview")
    out = capsys.readouterr().out
    assert "1 open handoff(s)" in out and "#1 date conflict" in out
    assert "1 screenshot(s) to check" in out and "CS-0003" in out and "DATE CONFLICT" in out
    assert "CS-0001  Al-Subiya  Mona" in out
    assert "Collect on arrival: 20 KWD security deposit" in out
    assert "Thu 1 Oct 2026" in out and "Ahmad  (CS-0002)" in out
    assert "WAITING FOR PAYMENT: 1 booking(s)" in out


def test_overview_reminds_to_complete_past_setups(service, db_path, capsys):
    book(service, "Ahmad", "99999999", booking_date=TUESDAY, choice="full")
    service.approve_payment("CS-0001")
    wednesday = lambda: datetime(2026, 9, 30, 10, 0, tzinfo=INFO.timezone)
    admin.main(["--db", str(db_path), "overview"], clock=wednesday)
    assert "Past setups to mark as completed: CS-0001" in capsys.readouterr().out
