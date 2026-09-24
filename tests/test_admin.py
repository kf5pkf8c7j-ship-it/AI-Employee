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
    assert "✓ Confirmation queued for the customer (terminal) - it goes out with cozysetup-outbox." in out
    assert "Reminder queued for Thu 1 Oct, 3 PM." in out
    assert "has not been messaged" not in out
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


# --- Step 6.4: messages to customers ------------------------------------------------------

from cozysetup import outbox as outbox_module          # noqa: E402
from cozysetup.database import OutboxStatus            # noqa: E402

MONA = ["add", "--date", "2026-10-01", "--location", "bnaider", "--name", "Mona",
        "--phone", "66666666", "--payment", "full"]


def later(day, hour, minute=0):
    moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=INFO.timezone)
    return lambda: moment


def outbox_row(service, number):
    return outbox_module.get_message(service.db, number)


# Truthful output after actions

def test_approve_says_the_reminder_is_skipped_when_confirmed_after_its_time(service, db_path, capsys):
    book(service, "Fahad", "55555555", booking_date=TUESDAY, choice="full")
    code = admin.main(["--db", str(db_path), "approve", "CS-0001", "-y"], clock=later(TUESDAY, 16))
    out = capsys.readouterr().out
    assert code == 0
    assert "✓ Confirmation queued for the customer (terminal) - it goes out with cozysetup-outbox." in out
    assert "No reminder: confirmed after its time (3 PM)." in out


def test_owner_paid_booking_says_send_yourself_with_the_phone(db_path, capsys):
    assert act(db_path, "y", *MONA, "--paid")[0] == 0
    assert ("📋 Send yourself: the confirmation now, and the reminder on Thu 1 Oct at 3 PM, "
            "to +96566666666. See: cozysetup-admin outbox") in capsys.readouterr().out


def test_owner_booking_approved_later_also_says_send_yourself(service, db_path, capsys):
    act(db_path, "y", *MONA)                                    # waiting for payment: no messages yet
    assert "Send yourself" not in capsys.readouterr().out
    act(db_path, "y", "approve", "CS-0001")
    assert "📋 Send yourself: the confirmation now" in capsys.readouterr().out


def test_cancel_counts_the_cancelled_messages(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    act(db_path, "y", "cancel", "CS-0001")
    out = capsys.readouterr().out
    assert "2 waiting messages cancelled. Tell the customer about the cancellation yourself." in out
    assert "has not been messaged" not in out


def test_cancel_of_a_waiting_booking_just_reminds_to_tell_the_customer(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    act(db_path, "y", "cancel", "CS-0001")
    out = capsys.readouterr().out
    assert "Tell the customer about the cancellation yourself." in out
    assert "waiting message" not in out


def test_reschedule_says_where_the_reminder_moved(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    act(db_path, "y", "reschedule", "CS-0001", "2026-10-08")
    assert "Reminder moved to Thu 8 Oct, 3 PM. Tell the customer about the new date yourself." in (
        capsys.readouterr().out)


def test_reject_still_says_the_customer_was_not_messaged(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.attach_payment_proof("CS-0001", "99999999", b"\xff\xd8\xff" + bytes(50))
    act(db_path, "y", "reject", "CS-0001")
    assert "The customer has not been messaged" in capsys.readouterr().out


# The outbox command

def test_outbox_lists_messages_waiting_for_the_sender(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    run(db_path, "outbox")
    out = capsys.readouterr().out
    assert "WAITING FOR THE SENDER (2)" in out
    assert "#1   confirmation  CS-0001 Ahmad  → Ahmad (terminal)   waiting · due now" in out
    assert "#2   reminder      CS-0001 Ahmad  → Ahmad (terminal)   waiting · Thu 1 Oct, 3 PM" in out
    assert "SEND YOURSELF" not in out and "FAILED" not in out
    assert "Your booking CS-0001 is confirmed!" not in out     # full text only for send-yourself


def test_outbox_send_yourself_due_now_and_later(service, db_path, capsys):
    act(db_path, "y", "add", "--date", "2026-10-02", "--location", "bnaider", "--name", "Mona",
        "--phone", "66666666", "--payment", "full", "--paid")
    capsys.readouterr()
    run(db_path, "outbox")
    out = capsys.readouterr().out
    assert "SEND YOURSELF - DUE NOW (1)" in out
    assert "SEND YOURSELF - LATER (1)" in out
    assert "#1   confirmation  CS-0001 Mona  → +96566666666   send yourself · due now" in out
    assert "        Your booking CS-0001 is confirmed!" in out                   # full text, ready to copy
    assert "        → when sent: cozysetup-admin mark-sent 1" in out
    assert "#2   reminder      CS-0001 Mona  → +96566666666   send yourself · Fri 2 Oct, 3 PM" in out


def test_outbox_shows_retrying_and_failed_messages(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    book(service, "Sara", "55555555", booking_date=date(2026, 10, 2))
    service.approve_payment("CS-0001")
    service.approve_payment("CS-0002")
    with service.db:   # CS-0001's confirmation: attempt 1 failed; CS-0002's: failed after 3
        service.db.execute("UPDATE outbox SET attempts = 1, last_error = 'network unreachable', "
                           "updated_at = ? WHERE id = 1", (NOW.isoformat(),))
        service.db.execute("UPDATE outbox SET status = 'failed', attempts = 3, "
                           "last_error = 'network unreachable' WHERE id = 3")
    run(db_path, "outbox")
    out = capsys.readouterr().out
    assert "waiting · attempt 1 failed: network unreachable, next try 14:05" in out
    assert "FAILED (1)" in out
    assert "failed after 3 attempts: network unreachable (see handoffs)" in out
    assert "→ if you contacted the customer yourself: cozysetup-admin mark-sent 3" in out


def test_outbox_hides_sent_and_cancelled_unless_all(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    service.cancel_booking("CS-0001")
    run(db_path, "outbox")
    assert "Nothing waiting or failed." in capsys.readouterr().out
    run(db_path, "outbox", "--all")
    out = capsys.readouterr().out
    assert "CANCELLED (2)" in out


def test_outbox_when_empty(db_path, capsys):
    run(db_path, "outbox", "--all")
    assert "The outbox is empty." in capsys.readouterr().out


# One message in full

def test_message_shows_everything(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    assert run(db_path, "message", "2") == 0
    out = capsys.readouterr().out
    assert "Message #2 - reminder for CS-0001 (Ahmad)" in out
    assert "To:          Ahmad  (channel: terminal)" in out
    assert "Send from:   Thu 1 Oct, 3 PM" in out
    assert "    Reminder: your CozySetup booking is today at Julaia, from 6 PM to 11 PM." in out


def test_message_unknown(db_path, capsys):
    assert run(db_path, "message", "99") == 1
    assert "No message #99" in capsys.readouterr().err


# mark-sent

def test_mark_sent_asks_first_then_records_it_as_sent_by_the_owner(service, db_path, capsys):
    act(db_path, "y", *MONA, "--paid")
    capsys.readouterr()
    code, questions = act(db_path, "y", "mark-sent", "1", "sent on WhatsApp")
    out = capsys.readouterr().out
    assert (code, questions) == (0, ["Confirm? [y/N] "])
    assert "Mark message #1 as sent? (confirmation for CS-0001, Mona, to +96566666666)" in out
    assert "✓ Message #1 marked as sent." in out
    assert outbox_row(service, 1).status is OutboxStatus.SENT
    owner_entries = [(e["actor"], e["details"]) for e in service.booking_history("CS-0001") if e["actor"] == "owner"]
    assert ("owner", "confirmation sent by the owner (sent on WhatsApp)") in owner_entries


def test_mark_sent_answer_no_changes_nothing(service, db_path):
    act(db_path, "y", *MONA, "--paid")
    assert act(db_path, "", "mark-sent", "1")[0] == 1
    assert outbox_row(service, 1).status is OutboxStatus.SEND_YOURSELF


def test_mark_sent_early_reminder_says_when_it_is_due(service, db_path, capsys):
    act(db_path, "y", *MONA, "--paid")
    capsys.readouterr()
    act(db_path, "y", "mark-sent", "2")
    assert "This reminder is due Thu 1 Oct, 3 PM - mark it as sent already?" in capsys.readouterr().out


def test_mark_sent_accepts_a_failed_message(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    with service.db:
        service.db.execute("UPDATE outbox SET status = 'failed', attempts = 3 WHERE id = 1")
    capsys.readouterr()
    assert act(db_path, "y", "mark-sent", "1", "called them")[0] == 0
    out = capsys.readouterr().out
    assert "The handoff about it stays open until you resolve it." in out
    assert outbox_row(service, 1).status is OutboxStatus.SENT
    assert "confirmation sent by the owner after automatic sending failed (called them)" in [
        e["details"] for e in service.booking_history("CS-0001")]


@pytest.mark.parametrize(
    ("prepare", "message"),
    [
        ("pending", "can't be marked as sent: the automatic sender handles it"),
        ("sent", "can't be marked as sent: it was already sent"),
        ("cancelled", "can't be marked as sent: it was cancelled"),
    ],
)
def test_mark_sent_refused_before_asking(service, db_path, capsys, prepare, message):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    with service.db:
        service.db.execute("UPDATE outbox SET status = ? WHERE id = 1", (prepare,))
    assert act(db_path, "y", "mark-sent", "1") == (1, [])
    assert message in capsys.readouterr().err


def test_mark_sent_unknown_message(db_path, capsys):
    assert act(db_path, "y", "mark-sent", "42") == (1, [])
    assert "No message #42" in capsys.readouterr().err


# overview and show

def test_overview_shows_messages_needing_attention(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    act(db_path, "y", "add", "--date", "2026-10-02", "--location", "bnaider", "--name", "Mona",
        "--phone", "66666666", "--payment", "full", "--paid")
    with service.db:
        service.db.execute("UPDATE outbox SET status = 'failed', attempts = 3 WHERE id = 1")
    capsys.readouterr()
    run(db_path, "overview")
    out = capsys.readouterr().out
    assert "  1 message(s) to send yourself now - see: outbox" in out
    assert "  1 message(s) failed to send - see: outbox, handoffs" in out
    assert "MESSAGES" in out
    assert "  Next automatic: reminder for CS-0001 on Thu 1 Oct, 3 PM" in out


def test_overview_without_messages(db_path, capsys):
    run(db_path, "overview")
    assert "  No messages waiting." in capsys.readouterr().out


def test_show_lists_the_bookings_messages(service, db_path, capsys):
    book(service, "Ahmad", "99999999")
    service.approve_payment("CS-0001")
    run(db_path, "show", "CS-0001")
    out = capsys.readouterr().out
    assert "  Messages:" in out
    assert "#1   confirmation  waiting · due now" in out
    assert "#2   reminder      waiting · Thu 1 Oct, 3 PM" in out
