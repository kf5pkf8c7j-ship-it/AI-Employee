"""Tests for confirmations and reminders on Instagram (Step 7.5) - pretend senders only,
made-up customers, temporary databases. Nothing is sent to Instagram."""

import itertools
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from cozysetup import admin, outbox, send_outbox
from cozysetup.bookings import BookingService
from cozysetup.business_info import load_business_info
from cozysetup.database import MIGRATIONS, SCHEMA_V1, SCHEMA_VERSION, OutboxKind, OutboxStatus, connect
from cozysetup.senders import (
    INSTAGRAM_MAX_TEXT_BYTES,
    InstagramSender,
    LogSender,
    SendError,
    default_senders,
    instagram_window_open,
)
from tests.test_instagram_worker import PretendOpenAI, Setup

INFO = load_business_info()
KUWAIT = INFO.timezone
MONDAY_2PM = datetime(2026, 9, 28, 14, 0, tzinfo=KUWAIT)
THURSDAY = date(2026, 10, 1)
CUSTOMER = "1234567890123456"     # a made-up Instagram id
IDS = itertools.count(1)


class PretendInstagram:
    """Records what would have been sent; returns an Instagram-like message id."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.sent = []

    def send(self, recipient, text):
        if self.errors:
            error = self.errors.pop(0)
            if error:
                raise error
        self.sent.append((recipient, text))
        return f"mid.outbox.{next(IDS)}"


class Clock:
    def __init__(self):
        self.now = MONDAY_2PM

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def service(tmp_path, clock):
    db = connect(tmp_path / "cozysetup.db")
    yield BookingService(db, INFO, clock=clock, proofs_dir=tmp_path / "proofs")
    db.close()


def instagram_booking(service, booking_date=THURSDAY):
    booking = service.create_booking(
        booking_date=booking_date, location_id="julaia", customer_name="Ahmad", customer_phone="99999999",
        payment_choice="deposit", channel="instagram", channel_user_id=CUSTOMER, language="en",
    )
    service.approve_payment(booking.reference)
    return service.get_booking(booking.reference)


def customer_wrote(service, at, *, paused=False):
    """The Instagram conversation, as the worker records it."""
    with service.db:
        service.db.execute(
            "INSERT INTO conversations (channel, channel_user_id, last_customer_message_at, ai_paused, "
            "created_at, updated_at) VALUES ('instagram', ?, ?, ?, ?, ?) "
            "ON CONFLICT (channel, channel_user_id) DO UPDATE SET last_customer_message_at = excluded.last_customer_message_at",
            (CUSTOMER, at.isoformat(timespec="seconds") if at else None, int(paused),
             MONDAY_2PM.isoformat(), MONDAY_2PM.isoformat()))


def message(service, booking, kind):
    return next(m for m in outbox.messages_for(service.db, booking.id) if m.kind is kind)


def deliver(service, sender):
    return [(r.message.kind.value, r.outcome) for r in outbox.deliver_due(service, {"instagram": sender})]


def history(service, booking):
    return [row["details"] for row in service.booking_history(booking.reference)]


# --- Inside the 24-hour window: sent automatically ---------------------------------------------

def test_inside_the_window_the_confirmation_is_sent_on_instagram(service):
    customer_wrote(service, MONDAY_2PM - timedelta(hours=1))
    booking = instagram_booking(service)
    sender = PretendInstagram()
    assert deliver(service, sender) == [("confirmation", "sent")]
    assert sender.sent == [(CUSTOMER, message(service, booking, OutboxKind.CONFIRMATION).text)]
    sent = message(service, booking, OutboxKind.CONFIRMATION)
    assert sent.status is OutboxStatus.SENT
    assert sent.external_id.startswith("mid.outbox.")          # so its echo is recognised as ours


def test_confirmations_are_sent_even_while_the_owner_handles_the_chat(service):
    customer_wrote(service, MONDAY_2PM - timedelta(minutes=5), paused=True)   # decision 1
    instagram_booking(service)
    assert deliver(service, PretendInstagram()) == [("confirmation", "sent")]


def test_the_reminder_goes_automatically_if_the_customer_wrote_in_the_last_24_hours(service, clock):
    customer_wrote(service, MONDAY_2PM)
    booking = instagram_booking(service)
    sender = PretendInstagram()
    deliver(service, sender)
    clock.now = outbox.reminder_time(INFO, booking)
    customer_wrote(service, clock.now - timedelta(hours=2))
    assert deliver(service, sender) == [("reminder", "sent")]


# --- Outside the window, or unknown: send yourself (decisions A and 2) -------------------------------

@pytest.mark.parametrize("last_message", [None, "no conversation", MONDAY_2PM - timedelta(hours=24)],
                         ids=["no last message time", "no conversation record", "exactly 24 hours ago"])
def test_outside_the_window_the_confirmation_is_left_for_the_owner(service, last_message):
    if last_message != "no conversation":
        customer_wrote(service, last_message)
    booking = instagram_booking(service)
    sender = PretendInstagram()
    assert deliver(service, sender) == [("confirmation", "send_yourself")]
    assert sender.sent == []                                  # never tried automatically
    left = message(service, booking, OutboxKind.CONFIRMATION)
    assert left.status is OutboxStatus.SEND_YOURSELF and left.attempts == 0
    assert "24-hour window" in left.last_error
    assert any("left for the owner to send" in line for line in history(service, booking))
    assert service.list_handoffs() == []                      # it's in the outbox list, not a handoff


def test_the_reminder_is_checked_on_the_day_not_when_confirmed(service, clock):
    customer_wrote(service, MONDAY_2PM)
    booking = instagram_booking(service)
    sender = PretendInstagram()
    assert deliver(service, sender) == [("confirmation", "sent")]
    assert message(service, booking, OutboxKind.REMINDER).status is OutboxStatus.PENDING
    clock.now = outbox.reminder_time(INFO, booking)          # three days later, no new message
    assert deliver(service, sender) == [("reminder", "send_yourself")]
    assert len(sender.sent) == 1


def test_if_the_window_closes_between_retries_it_becomes_send_yourself(service, clock):
    customer_wrote(service, MONDAY_2PM - timedelta(hours=23, minutes=58))
    instagram_booking(service)
    sender = PretendInstagram(SendError("Instagram busy"))
    assert deliver(service, sender) == [("confirmation", "retry")]
    clock.now += timedelta(minutes=5)
    assert deliver(service, sender) == [("confirmation", "send_yourself")]
    assert sender.sent == []


def test_the_owner_marks_an_instagram_message_sent_after_sending_it(service):
    booking = instagram_booking(service)
    deliver(service, PretendInstagram())
    left = message(service, booking, OutboxKind.CONFIRMATION)
    assert outbox.mark_sent_by_owner(service, left.id).status is OutboxStatus.SENT


def test_a_send_yourself_instagram_message_still_expires_when_too_late(service, clock):
    booking = instagram_booking(service)
    deliver(service, PretendInstagram())
    clock.now = datetime(2026, 10, 2, 9, 0, tzinfo=KUWAIT)   # the booking date has passed
    outbox.deliver_due(service, {"instagram": PretendInstagram()})
    assert message(service, booking, OutboxKind.CONFIRMATION).status is OutboxStatus.CANCELLED


def test_window_rule():
    db = connect(":memory:")
    assert not instagram_window_open(db, CUSTOMER, MONDAY_2PM)
    db.execute("INSERT INTO conversations (channel, channel_user_id, last_customer_message_at, created_at, "
               "updated_at) VALUES ('instagram', ?, ?, 'x', 'x')", (CUSTOMER, MONDAY_2PM.isoformat()))
    assert instagram_window_open(db, CUSTOMER, MONDAY_2PM + timedelta(hours=23, minutes=59))
    assert not instagram_window_open(db, CUSTOMER, MONDAY_2PM + timedelta(hours=24))
    assert not instagram_window_open(db, "someone else", MONDAY_2PM)
    db.close()


# --- Echoes: the worker knows the confirmation was ours ------------------------------------------------

def test_the_echo_of_a_confirmation_does_not_pause_the_ai(tmp_path):
    s = Setup(tmp_path / "cozysetup.db", client=PretendOpenAI())
    try:
        customer_wrote(s.service, s.clock() - timedelta(hours=1))
        booking = instagram_booking(s.service)
        outbox.deliver_due(s.service, {"instagram": PretendInstagram()})
        sent_id = message(s.service, booking, OutboxKind.CONFIRMATION).external_id
        s.receive(s.dm("Your booking CS-0001 is confirmed!", echo=True, mid=sent_id))
        [result] = s.worker.run_once()
        assert result.what == "echo"
        assert not s.conversation().ai_paused
    finally:
        s.close()


# --- The sender -----------------------------------------------------------------------------------------

def test_a_message_over_1000_bytes_is_refused_before_calling_instagram():
    calls = []
    sender = InstagramSender("TOKEN", post=lambda *args: calls.append(args) or {"message_id": "m"})
    with pytest.raises(SendError, match="1000") as refused:
        sender.send(CUSTOMER, "ح" * (INSTAGRAM_MAX_TEXT_BYTES // 2 + 1))
    assert refused.value.permanent and calls == []
    assert sender.send(CUSTOMER, "ح" * (INSTAGRAM_MAX_TEXT_BYTES // 2)) == "m"


def test_today_s_confirmation_and_reminder_fit_in_one_instagram_message(service):
    customer_wrote(service, MONDAY_2PM)
    booking = instagram_booking(service)
    for kind in (OutboxKind.CONFIRMATION, OutboxKind.REMINDER):
        assert len(message(service, booking, kind).text.encode("utf-8")) <= INSTAGRAM_MAX_TEXT_BYTES


def test_instagram_is_a_default_sender_only_with_a_token(tmp_path):
    without = default_senders(tmp_path / "x.log", lambda: MONDAY_2PM)
    assert set(without) == {"terminal", "eval"}
    with_token = default_senders(tmp_path / "x.log", lambda: MONDAY_2PM, instagram_token="TOKEN")
    assert isinstance(with_token["instagram"], InstagramSender)
    assert isinstance(with_token["terminal"], LogSender)


# --- The commands -------------------------------------------------------------------------------------

def test_the_outbox_command_reports_send_yourself(service, tmp_path, capsys, clock):
    instagram_booking(service)
    code = send_outbox.main(["--db", str(tmp_path / "cozysetup.db"), "run"],
                            senders={"instagram": PretendInstagram()}, clock=clock)
    out = capsys.readouterr().out
    assert code == 0
    assert "✋ send yourself  confirmation for CS-0001" in out
    assert "send yourself 1 (Instagram's 24-hour window is closed" in out


def test_without_a_token_waiting_instagram_messages_are_reported_and_handed_over(service, tmp_path, capsys,
                                                                                  clock):
    customer_wrote(service, MONDAY_2PM)
    instagram_booking(service)
    code = send_outbox.main(["--db", str(tmp_path / "cozysetup.db"), "run"], clock=clock)
    captured = capsys.readouterr()
    assert code == 2
    assert "No IG_ACCESS_TOKEN in .env: 2 waiting Instagram message(s)" in captured.err   # confirmation + reminder
    assert "no sender is set up for the 'instagram' channel" in captured.out
    [handoff] = service.list_handoffs()
    assert handoff.booking_id is not None


def test_admin_shows_who_to_write_to_on_instagram_and_why(service, tmp_path, capsys, clock):
    booking = instagram_booking(service)
    outbox.deliver_due(service, {"instagram": PretendInstagram()})
    assert admin.main(["--db", str(tmp_path / "cozysetup.db"), "outbox"], clock=clock) == 0
    out = capsys.readouterr().out
    [line] = [line for line in out.splitlines() if "confirmation" in line]
    assert f"→ Instagram DM to Ahmad ({booking.customer_phone})" in line
    assert CUSTOMER not in line                                 # the raw Instagram id means nothing to the owner
    assert "Why: Instagram's 24-hour window is closed" in out


# --- Database version 5 --------------------------------------------------------------------------------

def test_a_version_4_database_is_upgraded_keeping_its_outbox(tmp_path):
    path = tmp_path / "cozysetup.db"
    db = sqlite3.connect(path)
    db.executescript(f"BEGIN; {SCHEMA_V1} PRAGMA user_version = 1; COMMIT;")
    for version in (2, 3, 4):
        db.executescript(f"BEGIN; {MIGRATIONS[version]} PRAGMA user_version = {version}; COMMIT;")
    db.close()
    service = BookingService(connect(path), INFO, clock=lambda: MONDAY_2PM, proofs_dir=tmp_path / "p")
    booking = instagram_booking(service)
    assert message(service, booking, OutboxKind.CONFIRMATION).external_id is None
    assert service.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
    service.db.close()
    backup = sqlite3.connect(tmp_path / "cozysetup.db.before-v5.bak")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 4
    backup.close()


def test_two_messages_can_never_share_an_instagram_id(service):
    customer_wrote(service, MONDAY_2PM)
    booking = instagram_booking(service)
    deliver(service, PretendInstagram())
    sent = message(service, booking, OutboxKind.CONFIRMATION)
    with pytest.raises(sqlite3.IntegrityError):
        service.db.execute("UPDATE outbox SET external_id = ? WHERE id <> ?", (sent.external_id, sent.id))


# --- Echoes the worker recognises by their text (review fixes #1 and #2) ----------------------------------

@pytest.fixture
def s(tmp_path):
    setup = Setup(tmp_path / "worker.db", client=PretendOpenAI())
    yield setup
    setup.close()


def confirmation_text(service, booking):
    return message(service, booking, OutboxKind.CONFIRMATION).text


def owner_echo(s, words, mid="mid.typed.in.the.app"):
    """What Meta sends when a message is sent from @cozysetup.kw outside our code."""
    s.receive(s.dm(words, echo=True, mid=mid))
    [result] = s.worker.run_once()
    return result


def test_a_send_yourself_confirmation_sent_by_the_owner_does_not_pause_the_ai(s):
    booking = instagram_booking(s.service)                       # no recent message: send yourself
    outbox.deliver_due(s.service, {"instagram": PretendInstagram()})
    assert message(s.service, booking, OutboxKind.CONFIRMATION).status is OutboxStatus.SEND_YOURSELF

    result = owner_echo(s, confirmation_text(s.service, booking))
    assert result.what == "echo"
    assert s.conversation() is None                              # nothing paused - not even created
    assert s.inbound()[-1]["status"] == "ignored"
    assert "matched by its text" in s.inbound()[-1]["error"]


def test_a_send_yourself_reminder_sent_by_the_owner_does_not_pause_the_ai(s):
    customer_wrote(s.service, s.clock())
    booking = instagram_booking(s.service)
    outbox.deliver_due(s.service, {"instagram": PretendInstagram()})
    s.clock.now = outbox.reminder_time(INFO, booking)          # the booking day: window closed
    outbox.deliver_due(s.service, {"instagram": PretendInstagram()})
    reminder = message(s.service, booking, OutboxKind.REMINDER)
    assert reminder.status is OutboxStatus.SEND_YOURSELF
    assert owner_echo(s, reminder.text).what == "echo"
    assert not s.conversation().ai_paused


def test_the_echo_arriving_before_the_outbox_saved_the_id_does_not_pause_the_ai(s):
    """The timing gap: sent, but the outbox hasn't recorded Instagram's id yet."""
    customer_wrote(s.service, s.clock())
    booking = instagram_booking(s.service)
    confirmation = message(s.service, booking, OutboxKind.CONFIRMATION)
    with s.service.db:   # claimed and being sent: pending, one attempt, no id yet
        s.service.db.execute("UPDATE outbox SET attempts = 1 WHERE id = ?", (confirmation.id,))
    assert owner_echo(s, confirmation.text, mid="mid.not.recorded.yet").what == "echo"
    assert not s.conversation().ai_paused


def test_the_echo_of_a_duplicate_after_a_crash_does_not_pause_the_ai(s):
    customer_wrote(s.service, s.clock())
    booking = instagram_booking(s.service)
    outbox.deliver_due(s.service, {"instagram": PretendInstagram()})
    assert message(s.service, booking, OutboxKind.CONFIRMATION).status is OutboxStatus.SENT
    # The same text again, with an id we never recorded (sent before a crash).
    assert owner_echo(s, confirmation_text(s.service, booking), mid="mid.lost").what == "echo"
    assert not s.conversation().ai_paused


def test_a_failed_confirmation_sent_by_the_owner_does_not_pause_the_ai(s):
    customer_wrote(s.service, s.clock())
    booking = instagram_booking(s.service)
    outbox.deliver_due(s.service, {"instagram": PretendInstagram(SendError("blocked", permanent=True))})
    assert message(s.service, booking, OutboxKind.CONFIRMATION).status is OutboxStatus.FAILED
    assert owner_echo(s, confirmation_text(s.service, booking)).what == "echo"
    assert not s.conversation().ai_paused


def test_line_ending_style_and_outer_spaces_are_ignored_when_matching(s):
    booking = instagram_booking(s.service)
    outbox.deliver_due(s.service, {"instagram": PretendInstagram()})
    typed = "  " + confirmation_text(s.service, booking).replace("\n", "\r\n") + "\n"
    assert owner_echo(s, typed).what == "echo"


def test_the_owner_writing_anything_else_still_pauses_the_ai(s):
    booking = instagram_booking(s.service)
    outbox.deliver_due(s.service, {"instagram": PretendInstagram()})
    edited = confirmation_text(s.service, booking) + " See you there!"
    assert owner_echo(s, edited).what == "owner_replied"
    assert s.conversation().ai_paused


def test_a_cancelled_messages_text_does_not_count(s):
    booking = instagram_booking(s.service)
    text_before = confirmation_text(s.service, booking)
    s.service.cancel_booking(booking.reference)                 # its messages are cancelled
    assert message(s.service, booking, OutboxKind.CONFIRMATION).status is OutboxStatus.CANCELLED
    assert owner_echo(s, text_before).what == "owner_replied"
    assert s.conversation().ai_paused


def test_another_customers_confirmation_does_not_count(s):
    booking = instagram_booking(s.service)
    outbox.deliver_due(s.service, {"instagram": PretendInstagram()})
    s.receive(s.dm(confirmation_text(s.service, booking), echo=True, customer="9999999999999999"))
    [result] = s.worker.run_once()
    assert result.what == "owner_replied"


def test_a_chat_reply_still_being_sent_does_not_pause_the_ai(tmp_path):
    from tests.test_agent import answer, text
    from tests.test_instagram_worker import PretendSender
    s = Setup(tmp_path / "w.db", client=PretendOpenAI(answer(text("Hello!"))),
              sender=PretendSender(SendError("busy")))
    try:
        s.receive(s.dm("Hi"))
        s.worker.run_once()                                     # the disclosure waits for a retry
        disclosure = s.replies()[0]["text"]
        s.receive(s.dm(disclosure, echo=True, mid="mid.delivered.despite.the.error"))
        [result] = s.worker.run_once()
        assert result.what == "echo" and not s.conversation().ai_paused
    finally:
        s.close()


# --- The window is checked with the time right now (review fix #3) ------------------------------------

def test_the_window_is_checked_at_the_moment_of_sending_not_at_the_start_of_the_round(service, clock):
    customer_wrote(service, MONDAY_2PM)
    instagram_booking(service)
    clock.now = MONDAY_2PM + timedelta(hours=24, minutes=1)   # the window closed while the round was running
    sender = PretendInstagram()
    results = outbox.deliver_due(service, {"instagram": sender}, now=MONDAY_2PM + timedelta(hours=23, minutes=59))
    assert [r.outcome for r in results] == ["send_yourself"]
    assert sender.sent == []


# --- Tests can't reach the real token (review fix #4) ----------------------------------------------------

def test_the_instagram_worker_command_cannot_read_the_real_token_in_tests(tmp_path, capsys):
    from cozysetup.serve_instagram import main
    code = main(["work", "--once", "--db", str(tmp_path / "x.db")], client=PretendOpenAI())
    assert code == 1
    assert "no Instagram token in tests" in capsys.readouterr().err


def test_the_outbox_command_cannot_read_the_real_token_in_tests():
    from cozysetup.settings import MissingApiKey
    with pytest.raises(MissingApiKey, match="in tests"):
        send_outbox.load_instagram_token()
