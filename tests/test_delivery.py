"""Tests for delivering the outbox (Piece 6.3), with pretend senders - no real channels."""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from cozysetup import outbox, send_outbox
from cozysetup.bookings import BookingService
from cozysetup.business_info import load_business_info
from cozysetup.database import HandoffType, OutboxKind, OutboxStatus, connect
from cozysetup.senders import LOG_FILE_NAME, LogSender, SendError, default_senders

INFO = load_business_info()
KUWAIT = INFO.timezone
THURSDAY = date(2026, 10, 1)


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KUWAIT)


MONDAY_2PM = at(date(2026, 9, 28), 14)


# --- Pretend senders ----------------------------------------------------------------

class RecordingSender:
    """Always succeeds, and remembers exactly what it was asked to send."""
    def __init__(self):
        self.sent = []

    def send(self, recipient, text):
        self.sent.append((recipient, text))


class FlakySender(RecordingSender):
    """Fails with a temporary error the first `fail_times` times, then succeeds."""
    def __init__(self, fail_times):
        super().__init__()
        self.fail_times, self.calls = fail_times, 0

    def send(self, recipient, text):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise SendError("network unreachable")
        super().send(recipient, text)


class BrokenSender:
    """Always fails."""
    def __init__(self, permanent=False):
        self.permanent, self.calls = permanent, 0

    def send(self, recipient, text):
        self.calls += 1
        raise SendError("recipient does not exist" if self.permanent else "network unreachable",
                        permanent=self.permanent)


class CrashingSender:
    """Has a bug: raises something that isn't a SendError."""
    def send(self, recipient, text):
        raise KeyError("oops")


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(MONDAY_2PM)


@pytest.fixture
def service(clock, tmp_path):
    db = connect(":memory:")
    yield BookingService(db, INFO, clock=clock, proofs_dir=tmp_path / "proofs")
    db.close()


def confirmed_booking(service, booking_date=THURSDAY, channel="terminal"):
    booking = service.create_booking(
        booking_date=booking_date, location_id="julaia", customer_name="Ahmad", customer_phone="99999999",
        payment_choice="deposit", channel=channel, channel_user_id="customer-1", language="en",
    )
    service.approve_payment(booking.reference)
    return service.get_booking(booking.reference)


def message(service, booking, kind):
    return next(m for m in reversed(outbox.messages_for(service.db, booking.id)) if m.kind is kind)


def deliver(service, senders):
    return [(r.message.kind.value, r.outcome) for r in outbox.deliver_due(service, senders)]


# --- What is due ----------------------------------------------------------------------

def test_the_confirmation_goes_now_and_the_reminder_waits_for_its_time(service, clock):
    booking = confirmed_booking(service)
    sender = RecordingSender()

    assert deliver(service, {"terminal": sender}) == [("confirmation", "sent")]
    assert sender.sent == [("customer-1", message(service, booking, OutboxKind.CONFIRMATION).text)]
    assert deliver(service, {"terminal": sender}) == []                     # nothing more due yet

    clock.now = at(THURSDAY, 14, 59)
    assert deliver(service, {"terminal": sender}) == []
    clock.now = at(THURSDAY, 15, 0)
    assert deliver(service, {"terminal": sender}) == [("reminder", "sent")]
    assert sender.sent[-1][1].startswith("Reminder: your CozySetup booking is today at Julaia")


def test_a_sent_message_is_marked_and_recorded(service):
    booking = confirmed_booking(service)
    deliver(service, {"terminal": RecordingSender()})
    sent = message(service, booking, OutboxKind.CONFIRMATION)
    assert (sent.status, sent.attempts, sent.sent_at, sent.last_error) == (OutboxStatus.SENT, 1, MONDAY_2PM, None)
    history = [e["details"] for e in service.booking_history(booking.reference)]
    assert "confirmation sent via terminal to customer-1" in history


def test_send_yourself_messages_are_never_touched(service, clock):
    booking = service.create_owner_booking(
        booking_date=THURSDAY, location_id="bnaider", customer_name="Mona", customer_phone="66666666",
        payment_choice="full", paid=True,
    ).booking
    sender = RecordingSender()
    clock.now = at(THURSDAY, 16)
    assert deliver(service, {"terminal": sender, "owner": sender}) == []
    assert sender.sent == []
    assert {m.status for m in outbox.messages_for(service.db, booking.id)} == {OutboxStatus.SEND_YOURSELF}


def test_cancelled_messages_are_never_sent(service):
    booking = confirmed_booking(service)
    service.cancel_booking(booking.reference)
    sender = RecordingSender()
    assert deliver(service, {"terminal": sender}) == []
    assert sender.sent == []


# --- Re-checking the booking at send time ------------------------------------------------

def test_a_booking_no_longer_confirmed_is_not_sent_to(service):
    booking = confirmed_booking(service)
    with service.db:   # the booking changed without going through cancel (e.g. by hand)
        service.db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking.id,))
    sender = RecordingSender()
    results = outbox.deliver_due(service, {"terminal": sender})
    assert [(r.outcome, r.detail) for r in results] == [("cancelled", "the booking is cancelled")]
    assert sender.sent == []
    assert message(service, booking, OutboxKind.CONFIRMATION).status is OutboxStatus.CANCELLED


def test_no_reminder_after_the_setup_has_started(service, clock):
    booking = confirmed_booking(service)
    deliver(service, {"terminal": RecordingSender()})           # the confirmation
    clock.now = at(THURSDAY, 18, 0)                                  # the sender was down all afternoon
    sender = RecordingSender()
    results = outbox.deliver_due(service, {"terminal": sender})
    assert [(r.outcome, r.detail) for r in results] == [("cancelled", "too late: the setup has already started")]
    assert sender.sent == []


def test_no_confirmation_after_the_booking_date(service, clock):
    confirmed_booking(service)
    clock.now = at(date(2026, 10, 2), 9)
    sender = RecordingSender()
    outcomes = {(r.message.kind.value, r.outcome, r.detail) for r in outbox.deliver_due(service, {"terminal": sender})}
    assert ("confirmation", "cancelled", "too late: the booking date has passed") in outcomes
    assert sender.sent == []


def test_a_reminder_whose_booking_moved_is_not_sent(service, clock):
    booking = confirmed_booking(service)
    with service.db:   # moved without going through reschedule
        service.db.execute("UPDATE bookings SET booking_date = '2026-10-08' WHERE id = ?", (booking.id,))
    clock.now = at(THURSDAY, 15)
    results = outbox.deliver_due(service, {"terminal": RecordingSender()})
    assert ("reminder", "cancelled", "the booking was moved") in {
        (r.message.kind.value, r.outcome, r.detail) for r in results}


# --- Retries ----------------------------------------------------------------------------------

def test_a_temporary_failure_is_retried_after_5_then_30_minutes(service, clock):
    booking = confirmed_booking(service)
    sender = FlakySender(fail_times=2)

    assert deliver(service, {"terminal": sender}) == [("confirmation", "retry")]
    waiting = message(service, booking, OutboxKind.CONFIRMATION)
    assert (waiting.status, waiting.attempts, waiting.last_error) == (OutboxStatus.PENDING, 1, "network unreachable")

    clock.now = MONDAY_2PM + timedelta(minutes=4, seconds=59)
    assert deliver(service, {"terminal": sender}) == []                            # too soon
    clock.now = MONDAY_2PM + timedelta(minutes=5)
    assert deliver(service, {"terminal": sender}) == [("confirmation", "retry")]   # attempt 2 fails

    clock.now += timedelta(minutes=29)
    assert deliver(service, {"terminal": sender}) == []                            # too soon
    clock.now += timedelta(minutes=1)
    assert deliver(service, {"terminal": sender}) == [("confirmation", "sent")]    # attempt 3 works
    assert sender.calls == 3
    assert service.list_handoffs() == []


def test_three_failures_mean_failed_and_a_handoff_for_the_owner(service, clock):
    booking = confirmed_booking(service)
    sender = BrokenSender()
    deliver(service, {"terminal": sender})
    clock.now += timedelta(minutes=5)
    deliver(service, {"terminal": sender})
    clock.now += timedelta(minutes=30)
    assert deliver(service, {"terminal": sender}) == [("confirmation", "failed")]
    assert sender.calls == 3

    failed = message(service, booking, OutboxKind.CONFIRMATION)
    assert (failed.status, failed.attempts) == (OutboxStatus.FAILED, 3)
    [handoff] = service.list_handoffs()
    assert handoff.type is HandoffType.OTHER
    assert handoff.booking_id == booking.id
    assert handoff.summary == (
        "Could not send the confirmation for CS-0001 to customer-1 via terminal after 3 attempts "
        "(last error: network unreachable). Please contact the customer yourself.")

    clock.now += timedelta(hours=5)
    assert deliver(service, {"terminal": sender}) == []    # a failed message is never tried again
    assert sender.calls == 3


def test_a_permanent_failure_fails_at_once(service):
    booking = confirmed_booking(service)
    sender = BrokenSender(permanent=True)
    assert deliver(service, {"terminal": sender}) == [("confirmation", "failed")]
    assert sender.calls == 1
    assert "after 1 attempt (last error: recipient does not exist)" in service.list_handoffs()[0].summary
    assert message(service, booking, OutboxKind.CONFIRMATION).status is OutboxStatus.FAILED


def test_a_channel_without_a_sender_fails_with_a_handoff(service):
    confirmed_booking(service, channel="whatsapp")
    assert deliver(service, {"terminal": RecordingSender()}) == [("confirmation", "failed")]
    assert "no sender is set up for the 'whatsapp' channel" in service.list_handoffs()[0].summary


def test_a_crashing_sender_is_treated_as_a_temporary_failure(service):
    booking = confirmed_booking(service)
    assert deliver(service, {"terminal": CrashingSender()}) == [("confirmation", "retry")]
    assert message(service, booking, OutboxKind.CONFIRMATION).last_error == "KeyError: 'oops'"


def test_one_failing_message_does_not_stop_the_others(service):
    confirmed_booking(service, channel="whatsapp")                      # no sender: fails
    confirmed_booking(service, booking_date=date(2026, 10, 8))          # terminal: fine
    sender = RecordingSender()
    assert sorted(deliver(service, {"terminal": sender})) == [("confirmation", "failed"), ("confirmation", "sent")]
    assert len(sender.sent) == 1


# --- Claiming: never sent twice by two senders at once ------------------------------------------

def test_only_one_sender_process_can_claim_a_message(service):
    booking = confirmed_booking(service)
    pending = message(service, booking, OutboxKind.CONFIRMATION)
    first, second = RecordingSender(), RecordingSender()

    # Process 1 claims and sends; process 2 read the same message a moment earlier.
    assert outbox._deliver_one(service, {"terminal": first}, pending, MONDAY_2PM).outcome == "sent"
    assert outbox._deliver_one(service, {"terminal": second}, pending, MONDAY_2PM) is None
    assert (len(first.sent), len(second.sent)) == (1, 0)


def test_a_crash_after_claiming_means_it_is_tried_again_later(service, clock):
    booking = confirmed_booking(service)
    with service.db:   # claimed (attempt 1), then the process died before sending
        service.db.execute("UPDATE outbox SET attempts = 1, updated_at = ? WHERE booking_id = ? AND kind = ?",
                           (MONDAY_2PM.isoformat(), booking.id, "confirmation"))
    clock.now += timedelta(minutes=5)
    sender = RecordingSender()
    assert deliver(service, {"terminal": sender}) == [("confirmation", "sent")]
    assert message(service, booking, OutboxKind.CONFIRMATION).attempts == 2


# --- The log sender -------------------------------------------------------------------------

def test_the_log_sender_writes_what_the_customer_would_receive(tmp_path, service):
    log = tmp_path / LOG_FILE_NAME
    confirmed_booking(service)
    deliver(service, default_senders(log, service.now))
    assert log.read_text(encoding="utf-8") == (
        "2026-09-28 14:00  terminal → customer-1\n"
        "  Your booking CS-0001 is confirmed!\n"
        "  Julaia, Thursday 1 October 2026, 6 PM – 11 PM.\n"
        "  On the booking day, please pay: 25 KWD remaining + 20 KWD security deposit.\n\n"
    )


def test_only_the_not_yet_connected_channels_use_the_log():
    senders = default_senders(Path("x.log"), lambda: MONDAY_2PM)
    assert set(senders) == {"terminal", "eval"}
    assert all(isinstance(sender, LogSender) for sender in senders.values())


# --- The command -------------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path, clock):
    path = tmp_path / "practice.db"
    db = connect(path)
    confirmed_booking(BookingService(db, INFO, clock=clock, proofs_dir=tmp_path / "proofs"))
    db.close()
    return path


def test_run_sends_what_is_due_and_summarises(db_path, clock, capsys):
    assert send_outbox.main(["--db", str(db_path), "run"], clock=clock) == 0
    out = capsys.readouterr().out
    assert "✓ sent  confirmation for CS-0001: via terminal to customer-1" in out
    assert "sent 1 · retry later 0 · failed 0 · not sent 0" in out
    assert (db_path.parent / LOG_FILE_NAME).exists()          # the log sits next to the database

    assert send_outbox.main(["--db", str(db_path), "run"], clock=clock) == 0
    assert "Nothing due." in capsys.readouterr().out


def test_run_exits_with_2_when_something_failed(db_path, clock, capsys):
    assert send_outbox.main(["--db", str(db_path), "run"], clock=clock,
                            senders={"terminal": BrokenSender(permanent=True)}) == 2
    assert "a handoff was created for each failure" in capsys.readouterr().out


def test_watch_checks_again_and_again_and_only_speaks_when_something_happens(db_path, clock, capsys):
    naps = []
    code = send_outbox.main(["--db", str(db_path), "watch", "--every", "30"], clock=clock,
                            sleep=naps.append, max_rounds=3)
    out = capsys.readouterr().out
    assert code == 0
    assert naps == [30, 30]                                 # waited between the 3 rounds
    assert out.count("✓ sent") == 1                         # sent once, then quiet
    assert "Watching the outbox every 30 seconds" in out


def test_watch_stops_cleanly_on_ctrl_c(db_path, clock, capsys):
    def interrupted(seconds):
        raise KeyboardInterrupt
    assert send_outbox.main(["--db", str(db_path), "watch"], clock=clock, sleep=interrupted) == 0
    assert "Stopped." in capsys.readouterr().out


# --- Fix 1: one bad message or round never stops the rest --------------------------------

def test_one_bad_message_does_not_stop_the_other_due_messages(service, monkeypatch):
    first = confirmed_booking(service)
    second = confirmed_booking(service, booking_date=date(2026, 10, 8))
    real = service.get_booking_by_id

    def unreadable_first_booking(booking_id):
        if booking_id == first.id:
            raise RuntimeError("database is locked")
        return real(booking_id)
    monkeypatch.setattr(service, "get_booking_by_id", unreadable_first_booking)

    sender = RecordingSender()
    results = outbox.deliver_due(service, {"terminal": sender})
    assert [(r.reference, r.outcome) for r in results] == [("message #1", "error"), ("CS-0002", "sent")]
    assert results[0].detail == "RuntimeError: database is locked"
    assert len(sender.sent) == 1

    # The bad message was rolled back: still pending, no attempt used, tried again next run.
    stuck = message(service, first, OutboxKind.CONFIRMATION)
    assert (stuck.status, stuck.attempts) == (OutboxStatus.PENDING, 0)
    monkeypatch.setattr(service, "get_booking_by_id", real)
    assert deliver(service, {"terminal": sender}) == [("confirmation", "sent")]


def test_run_reports_errors_and_exits_with_2(db_path, clock, capsys, monkeypatch):
    def broken_lookup(self, booking_id):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(BookingService, "get_booking_by_id", broken_lookup)
    assert send_outbox.main(["--db", str(db_path), "run"], clock=clock) == 2
    out = capsys.readouterr().out
    assert "⚠ error  confirmation for message #1: RuntimeError: database is locked" in out
    assert "errors 1 (those messages stay pending and are tried again next run)" in out


def test_watch_survives_a_failing_round_and_carries_on(db_path, clock, capsys, monkeypatch):
    real = outbox.deliver_due
    rounds = []

    def fails_the_first_time(service, senders, now=None):
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("database is locked")
        return real(service, senders, now)
    monkeypatch.setattr(outbox, "deliver_due", fails_the_first_time)

    code = send_outbox.main(["--db", str(db_path), "watch"], clock=clock, sleep=lambda s: None, max_rounds=3)
    captured = capsys.readouterr()
    assert code == 0
    assert len(rounds) == 3                                             # it kept going
    assert "⚠ this round failed: RuntimeError: database is locked - trying again next round" in captured.err
    assert "✓ sent  confirmation for CS-0001" in captured.out            # round 2 still delivered


# --- Fix 2: a message cancelled while send() runs stays cancelled ------------------------------

class CancelsWhileSending:
    """While the message is being sent, the owner cancels the booking. Then the send succeeds or fails."""
    def __init__(self, service, reference, error=None):
        self.service, self.reference, self.error = service, reference, error

    def send(self, recipient, text):
        self.service.cancel_booking(self.reference, "customer called to cancel")
        if self.error:
            raise self.error


@pytest.mark.parametrize(
    ("error", "what"),
    [
        (None, "was delivered"),
        (SendError("recipient does not exist", permanent=True), "could not be delivered (recipient does not exist)"),
        (SendError("network unreachable"), "could not be delivered (network unreachable)"),
    ],
    ids=["send succeeded", "permanent failure", "temporary failure"],
)
def test_cancelled_during_sending_stays_cancelled_with_no_handoff(service, error, what):
    booking = confirmed_booking(service)
    results = outbox.deliver_due(service, {"terminal": CancelsWhileSending(service, booking.reference, error)})

    assert [(r.outcome, r.detail) for r in results] == [
        ("cancelled", f"cancelled while sending - the confirmation {what}")]
    confirmation = message(service, booking, OutboxKind.CONFIRMATION)
    assert confirmation.status is OutboxStatus.CANCELLED          # not overwritten
    assert confirmation.last_error is None                         # not overwritten either
    assert service.list_handoffs() == []                           # no failure handoff
    history = [e["details"] for e in service.booking_history(booking.reference)]
    assert f"confirmation {what}, but it had been cancelled while sending" in history


def test_a_cancelled_message_is_not_retried(service, clock):
    booking = confirmed_booking(service)
    sender = CancelsWhileSending(service, booking.reference, SendError("network unreachable"))
    outbox.deliver_due(service, {"terminal": sender})
    clock.now += timedelta(hours=1)
    assert deliver(service, {"terminal": RecordingSender()}) == []


# --- Fix 3 (decision #11): no reminder for a confirmation that was never delivered -----------------

def test_a_confirmation_failing_after_3_attempts_cancels_its_reminder(service, clock):
    booking = confirmed_booking(service)
    sender = BrokenSender()
    deliver(service, {"terminal": sender})
    clock.now += timedelta(minutes=5)
    deliver(service, {"terminal": sender})
    clock.now += timedelta(minutes=30)
    [result] = outbox.deliver_due(service, {"terminal": sender})

    assert result.outcome == "failed"
    assert result.detail.endswith("handoff #1 created; its reminder was cancelled")
    assert message(service, booking, OutboxKind.REMINDER).status is OutboxStatus.CANCELLED
    assert len(service.list_handoffs()) == 1                       # the failure handoff is kept
    assert "reminder cancelled: the confirmation was never delivered" in [
        e["details"] for e in service.booking_history(booking.reference)]

    clock.now = at(THURSDAY, 15)                                   # the reminder's time comes
    assert deliver(service, {"terminal": RecordingSender()}) == []


def test_a_permanently_failed_confirmation_also_cancels_its_reminder(service):
    booking = confirmed_booking(service)
    deliver(service, {"terminal": BrokenSender(permanent=True)})
    assert message(service, booking, OutboxKind.REMINDER).status is OutboxStatus.CANCELLED


def test_a_confirmation_that_is_only_retrying_keeps_its_reminder(service):
    booking = confirmed_booking(service)
    deliver(service, {"terminal": BrokenSender()})                 # attempt 1 of 3 failed
    assert message(service, booking, OutboxKind.REMINDER).status is OutboxStatus.PENDING


def test_a_confirmation_delivered_after_retries_keeps_its_reminder(service, clock):
    booking = confirmed_booking(service)
    sender = FlakySender(fail_times=1)
    deliver(service, {"terminal": sender})
    clock.now += timedelta(minutes=5)
    assert deliver(service, {"terminal": sender}) == [("confirmation", "sent")]
    assert message(service, booking, OutboxKind.REMINDER).status is OutboxStatus.PENDING


def test_a_failed_reminder_does_not_cancel_anything_else(service, clock):
    booking = confirmed_booking(service)
    deliver(service, {"terminal": RecordingSender()})              # confirmation delivered
    clock.now = at(THURSDAY, 15)
    deliver(service, {"terminal": BrokenSender(permanent=True)})   # reminder fails
    statuses = {m.kind.value: m.status.value for m in outbox.messages_for(service.db, booking.id)}
    assert statuses == {"confirmation": "sent", "reminder": "failed"}
    assert len(service.list_handoffs()) == 1
