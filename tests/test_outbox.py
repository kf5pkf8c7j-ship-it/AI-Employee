"""Tests for queueing confirmations and reminders (Piece 6.2) - nothing is sent yet."""

import json
from dataclasses import replace
from datetime import date, datetime

import pytest

from cozysetup import outbox
from cozysetup.bookings import BookingRefused, BookingService, RefusalReason
from cozysetup.business_info import Reminders, load_business_info
from cozysetup.database import BookingStatus, Language, OutboxKind, OutboxStatus, connect
from cozysetup.tools import Conversation, Tools, tool_definitions

INFO = load_business_info()
KUWAIT = INFO.timezone
MONDAY_2PM = datetime(2026, 9, 28, 14, 0, tzinfo=KUWAIT)
MONDAY, TUESDAY, THURSDAY = date(2026, 9, 28), date(2026, 9, 29), date(2026, 10, 1)
NEXT_THURSDAY = date(2026, 10, 8)
JPG = b"\xff\xd8\xff\xe0" + bytes(100)


class Clock:
    """A clock the test can move forward."""
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


def ai_booking(service, booking_date=THURSDAY, language="en", payment="deposit"):
    return service.create_booking(
        booking_date=booking_date, location_id="julaia", customer_name="Ahmad", customer_phone="99999999",
        payment_choice=payment, channel="terminal", channel_user_id="customer-1", language=language,
    )


def owner_booking(service, booking_date=THURSDAY, paid=True):
    return service.create_owner_booking(
        booking_date=booking_date, location_id="bnaider", customer_name="Mona", customer_phone="66666666",
        payment_choice="full", paid=paid,
    ).booking


def messages(service, booking):
    return {(m.kind.value, m.status.value): m for m in outbox.messages_for(service.db, booking.id)}


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KUWAIT)


# --- Approving queues a confirmation and a reminder ---------------------------------

def test_approval_queues_the_owners_confirmation_now_and_reminder_on_the_day(service):
    booking = ai_booking(service)
    service.approve_payment(booking.reference)

    queued = messages(service, booking)
    assert set(queued) == {("confirmation", "pending"), ("reminder", "pending")}

    confirmation = queued["confirmation", "pending"]
    assert confirmation.send_after == MONDAY_2PM
    assert (confirmation.channel, confirmation.recipient) == ("terminal", "customer-1")
    assert confirmation.text == (
        "Your booking CS-0001 is confirmed!\n"
        "Julaia, Thursday 1 October 2026, 6 PM – 11 PM.\n"
        "On the booking day, please pay: 25 KWD remaining + 20 KWD security deposit."
    )

    reminder = queued["reminder", "pending"]
    assert reminder.send_after == at(THURSDAY, 15)            # 6 PM start - 3 hours
    assert reminder.text == (
        "Reminder: your CozySetup booking is today at Julaia, from 6 PM to 11 PM.\n"
        "Please remember to pay on arrival: 25 KWD remaining + 20 KWD security deposit."
    )


def test_full_payment_messages_mention_only_the_security_deposit(service):
    booking = ai_booking(service, payment="full")
    service.approve_payment(booking.reference)
    assert messages(service, booking)["confirmation", "pending"].text.endswith(
        "On the booking day, please pay: 20 KWD security deposit.")


def test_messages_are_in_english_for_now_even_for_arabic_customers(service):
    booking = ai_booking(service, language="ar")
    service.approve_payment(booking.reference)
    assert {m.language for m in outbox.messages_for(service.db, booking.id)} == {Language.ENGLISH}
    assert service.get_booking(booking.reference).language is Language.ARABIC     # kept for later


def test_the_queueing_is_recorded_in_the_booking_history(service):
    booking = ai_booking(service)
    service.approve_payment(booking.reference)
    details = [e["details"] for e in service.booking_history(booking.reference) if e["event"] == "outbox"]
    assert details == ["confirmation queued via terminal, from 28 Sep 14:00",
                       "reminder queued via terminal, from 01 Oct 15:00"]


def test_the_reminder_time_follows_the_business_file(service):
    booking = ai_booking(service)
    five_hours = replace(INFO, reminders=Reminders(hours_before_start=5))
    assert outbox.reminder_time(five_hours, booking) == at(THURSDAY, 13)


# --- Only confirmed bookings get messages ------------------------------------------

def test_waiting_bookings_get_no_messages(service):
    booking = ai_booking(service)
    service.attach_payment_proof(booking.reference, "99999999", JPG)
    assert outbox.messages_for(service.db, booking.id) == []


def test_a_refused_approval_queues_nothing(service):
    first, second = ai_booking(service), ai_booking(service)
    service.approve_payment(first.reference)
    with pytest.raises(BookingRefused):
        service.approve_payment(second.reference)                 # date already taken
    assert outbox.messages_for(service.db, second.id) == []


def test_if_queueing_fails_the_approval_is_undone_too(service, monkeypatch):
    booking = ai_booking(service)

    def broken(*args, **kwargs):
        raise RuntimeError("outbox unavailable")
    monkeypatch.setattr(outbox, "queue_confirmation_and_reminder", broken)

    with pytest.raises(RuntimeError):
        service.approve_payment(booking.reference)
    assert service.get_booking(booking.reference).status is BookingStatus.PENDING_PAYMENT
    assert outbox.messages_for(service.db, booking.id) == []


# --- The reminder is skipped when confirmed after its time --------------------------

def test_confirmed_on_the_day_before_the_reminder_time_gets_a_reminder(service, clock):
    booking = ai_booking(service, booking_date=TUESDAY, payment="full")
    clock.now = at(TUESDAY, 14, 59)
    service.approve_payment(booking.reference)
    assert ("reminder", "pending") in messages(service, booking)


def test_confirmed_after_the_reminder_time_gets_no_reminder(service, clock):
    booking = ai_booking(service, booking_date=TUESDAY, payment="full")
    clock.now = at(TUESDAY, 15, 0)
    service.approve_payment(booking.reference)
    assert set(messages(service, booking)) == {("confirmation", "pending")}
    details = [e["details"] for e in service.booking_history(booking.reference) if e["event"] == "outbox"]
    assert details[-1] == "reminder skipped: confirmed after the reminder time (29 Sep 15:00)"


def test_same_day_owner_booking_late_in_the_day_gets_no_reminder(service, clock):
    clock.now = at(MONDAY, 16)
    booking = owner_booking(service, booking_date=MONDAY)
    assert set(messages(service, booking)) == {("confirmation", "send_yourself")}


# --- Owner bookings: "send yourself" -----------------------------------------------

def test_owner_paid_booking_queues_both_messages_for_the_owner_to_send(service):
    booking = owner_booking(service)
    queued = messages(service, booking)
    assert set(queued) == {("confirmation", "send_yourself"), ("reminder", "send_yourself")}
    assert {m.recipient for m in queued.values()} == {"+96566666666"}   # the customer's phone
    assert queued["confirmation", "send_yourself"].text.startswith("Your booking CS-0001 is confirmed!")


def test_owner_unpaid_booking_gets_messages_once_approved(service):
    booking = owner_booking(service, paid=False)
    assert outbox.messages_for(service.db, booking.id) == []
    service.approve_payment(booking.reference)
    assert set(messages(service, booking)) == {("confirmation", "send_yourself"), ("reminder", "send_yourself")}


# --- Cancel, complete, reschedule --------------------------------------------------

def test_cancelling_cancels_every_waiting_message(service):
    booking = ai_booking(service)
    service.approve_payment(booking.reference)
    service.cancel_booking(booking.reference)
    assert {m.status for m in outbox.messages_for(service.db, booking.id)} == {OutboxStatus.CANCELLED}
    assert "2 waiting message(s) cancelled: booking cancelled" in [
        e["details"] for e in service.booking_history(booking.reference)]


def test_cancelling_a_waiting_booking_has_nothing_to_cancel(service):
    booking = ai_booking(service)
    service.cancel_booking(booking.reference)
    assert not [e for e in service.booking_history(booking.reference) if e["event"] == "outbox"]


def test_completing_cancels_a_reminder_not_yet_sent(service, clock):
    booking = ai_booking(service)
    service.approve_payment(booking.reference)
    clock.now = at(THURSDAY, 12)                      # before the 15:00 reminder
    service.complete_booking(booking.reference)
    assert messages(service, booking)[("reminder", "cancelled")]


def test_rescheduling_moves_the_reminder_and_sends_no_new_confirmation(service):
    booking = ai_booking(service)
    service.approve_payment(booking.reference)
    service.reschedule_booking(booking.reference, NEXT_THURSDAY)

    all_messages = outbox.messages_for(service.db, booking.id)
    waiting = [m for m in all_messages if m.status is OutboxStatus.PENDING]
    assert [(m.kind, m.send_after) for m in waiting] == [(OutboxKind.REMINDER, at(NEXT_THURSDAY, 15))]
    assert "Thursday 8 October" not in waiting[0].text        # the reminder says "today"
    assert "Julaia" in waiting[0].text
    cancelled = sorted(m.kind.value for m in all_messages if m.status is OutboxStatus.CANCELLED)
    assert cancelled == ["confirmation", "reminder"]            # the old ones mention the old date


def test_rescheduling_twice_still_leaves_exactly_one_waiting_reminder(service):
    booking = ai_booking(service)
    service.approve_payment(booking.reference)
    service.reschedule_booking(booking.reference, NEXT_THURSDAY)
    service.reschedule_booking(booking.reference, THURSDAY)
    waiting = [m for m in outbox.messages_for(service.db, booking.id) if m.status is OutboxStatus.PENDING]
    assert [(m.kind, m.send_after) for m in waiting] == [(OutboxKind.REMINDER, at(THURSDAY, 15))]


def test_rescheduling_to_today_after_the_reminder_time_skips_the_reminder(service, clock):
    booking = ai_booking(service)
    service.approve_payment(booking.reference)
    clock.now = at(MONDAY, 16)
    service.reschedule_booking(booking.reference, MONDAY)
    assert not [m for m in outbox.messages_for(service.db, booking.id) if m.status is OutboxStatus.PENDING]


def test_rescheduling_a_waiting_booking_queues_nothing(service):
    booking = ai_booking(service)
    service.reschedule_booking(booking.reference, NEXT_THURSDAY)
    assert outbox.messages_for(service.db, booking.id) == []


def test_list_messages_by_status_in_due_order(service):
    first, second = ai_booking(service), ai_booking(service, booking_date=NEXT_THURSDAY)
    service.approve_payment(second.reference)
    service.cancel_booking(first.reference)
    waiting = outbox.list_messages(service.db, (OutboxStatus.PENDING,))
    assert [(m.booking_id, m.kind.value) for m in waiting] == [(second.id, "confirmation"), (second.id, "reminder")]


# --- The customer's language, from the AI booking flow ------------------------------

BOOKING = {"date": "2026-10-01", "location_id": "julaia", "customer_name": "Ahmad",
           "customer_phone": "99999999", "payment_choice": "deposit"}


def ai_tools(service):
    return Tools(service, Conversation("terminal", "customer-1"))


@pytest.mark.parametrize("language", ["en", "ar", "arabizi"])
def test_the_ai_records_the_customers_language(service, language):
    tools = ai_tools(service)
    tools.run("show_booking_summary", BOOKING)
    result = tools.run("create_booking", {**BOOKING, "customer_language": language})
    assert json.loads(result.content)["saved"] is True
    assert service.get_booking("CS-0001").language is Language(language)


def test_the_language_is_only_asked_when_creating_the_booking():
    by_name = {tool["name"]: tool for tool in tool_definitions(INFO)}
    create = by_name["create_booking"]["input_schema"]["properties"]["customer_language"]
    assert create["enum"] == ["en", "ar", "arabizi"]
    assert "customer_language" not in by_name["show_booking_summary"]["input_schema"]["properties"]


def test_a_booking_without_a_language_is_still_created(service):
    tools = ai_tools(service)
    tools.run("show_booking_summary", BOOKING)
    tools.run("create_booking", BOOKING)
    assert service.get_booking("CS-0001").language is None


def test_an_unknown_language_is_refused_and_nothing_saved(service):
    with pytest.raises(BookingRefused) as refused:
        ai_booking(service, language="french")
    assert refused.value.reason is RefusalReason.INVALID_LANGUAGE
    assert service.list_bookings() == []


def test_owner_bookings_have_no_recorded_language(service):
    assert owner_booking(service).language is None


# --- The owner marks a message as sent (Step 6.4) -------------------------------------

def test_owner_marks_a_send_yourself_message_as_sent(service):
    booking = owner_booking(service)
    confirmation = messages(service, booking)["confirmation", "send_yourself"]
    marked = outbox.mark_sent_by_owner(service, confirmation.id, "sent on WhatsApp")
    assert (marked.status, marked.sent_at) == (OutboxStatus.SENT, MONDAY_2PM)
    entry = [e for e in service.booking_history(booking.reference) if e["actor"] == "owner"][-1]
    assert entry["details"] == "confirmation sent by the owner (sent on WhatsApp)"


def test_owner_marks_a_failed_message_as_sent(service):
    booking = ai_booking(service)
    service.approve_payment(booking.reference)
    with service.db:
        service.db.execute("UPDATE outbox SET status = 'failed' WHERE booking_id = ? AND kind = 'confirmation'",
                           (booking.id,))
    confirmation = messages(service, booking)["confirmation", "failed"]
    assert outbox.mark_sent_by_owner(service, confirmation.id).status is OutboxStatus.SENT
    assert "confirmation sent by the owner after automatic sending failed" in [
        e["details"] for e in service.booking_history(booking.reference)]


@pytest.mark.parametrize(
    ("status", "reason"),
    [("pending", "the automatic sender handles it"), ("sent", "it was already sent"),
     ("cancelled", "it was cancelled")],
)
def test_owner_cannot_mark_other_messages_as_sent(service, status, reason):
    booking = ai_booking(service)
    service.approve_payment(booking.reference)
    confirmation = messages(service, booking)["confirmation", "pending"]
    with service.db:
        service.db.execute("UPDATE outbox SET status = ? WHERE id = ?", (status, confirmation.id))
    with pytest.raises(outbox.OutboxRefused, match=reason):
        outbox.mark_sent_by_owner(service, confirmation.id)


def test_owner_cannot_mark_sent_when_the_booking_is_no_longer_confirmed(service):
    booking = owner_booking(service)
    confirmation = messages(service, booking)["confirmation", "send_yourself"]
    with service.db:   # changed by hand, bypassing cancel (which would have cancelled the message)
        service.db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking.id,))
    with pytest.raises(outbox.OutboxRefused, match="CS-0001 is cancelled"):
        outbox.mark_sent_by_owner(service, confirmation.id)


def test_owner_cannot_mark_an_unknown_message(service):
    with pytest.raises(outbox.OutboxRefused, match="No message #7"):
        outbox.mark_sent_by_owner(service, 7)
