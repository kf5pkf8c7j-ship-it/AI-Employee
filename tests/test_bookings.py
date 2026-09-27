"""Tests for the booking service."""

from datetime import date, datetime

import pytest

from cozysetup.bookings import (
    MAX_PROOF_BYTES,
    BookingRefused,
    BookingService,
    DateStatus,
    RefusalReason,
    detect_proof_file_type,
    format_reference,
    normalize_phone,
)
from cozysetup.business_info import load_business_info
from cozysetup.database import BookingStatus, HandoffStatus, HandoffType, connect
from cozysetup.rules import Amounts, PaymentChoice

INFO = load_business_info()

# Pretend it is Monday 28 September 2026, 2 PM in Kuwait.
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=INFO.timezone)
SUNDAY = date(2026, 9, 27)
MONDAY = date(2026, 9, 28)
TUESDAY = date(2026, 9, 29)
THURSDAY = date(2026, 10, 1)

DEPOSIT_AMOUNTS = Amounts(rental_price=50_000, amount_now=25_000, remaining_on_day=25_000, security_deposit=20_000)
FULL_AMOUNTS = Amounts(rental_price=50_000, amount_now=50_000, remaining_on_day=0, security_deposit=20_000)


@pytest.fixture
def db():
    connection = connect(":memory:")
    yield connection
    connection.close()


@pytest.fixture
def service(db, tmp_path):
    return BookingService(db, INFO, clock=lambda: NOW, proofs_dir=tmp_path / "proofs")


def book(service, **changes):
    """Create a valid booking, with any argument changed via **changes."""
    request = {
        "booking_date": THURSDAY, "location_id": "julaia", "customer_name": "Ahmad",
        "customer_phone": "99999999", "payment_choice": "deposit",
        "channel": "terminal", "channel_user_id": "test",
    }
    request.update(changes)
    return service.create_booking(**request)


def confirm_directly(db, reference):
    """Stand-in for the owner's approval until Piece 2.5 builds it."""
    set_status_directly(db, reference, "confirmed")


def set_status_directly(db, reference, status):
    with db:
        db.execute("UPDATE bookings SET status = ? WHERE reference = ?", (status, reference))


# The first bytes of real files of each type (the rest doesn't matter here).
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
PDF = b"%PDF-1.7" + b"\x00" * 100
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 100
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 100
PROGRAM = b"MZ\x90\x00" + b"\x00" * 100   # a Windows program, whatever its name says


# --- Time -----------------------------------------------------------------------

def test_today_is_kuwait_time_even_if_the_clock_is_in_another_timezone(db):
    from zoneinfo import ZoneInfo
    # 11 PM Sunday in London is already 1 AM Monday in Kuwait.
    london = datetime(2026, 9, 27, 23, 0, tzinfo=ZoneInfo("Europe/London"))
    assert BookingService(db, INFO, clock=lambda: london).today() == MONDAY


# --- Availability ------------------------------------------------------------------

def test_past_date(service):
    assert service.check_availability(SUNDAY).status is DateStatus.PAST


def test_same_day(service):
    assert service.check_availability(MONDAY).status is DateStatus.SAME_DAY


def test_tomorrow_is_available_with_full_payment_only(service):
    availability = service.check_availability(TUESDAY)
    assert availability.status is DateStatus.AVAILABLE
    assert availability.options == {PaymentChoice.FULL: FULL_AMOUNTS}


def test_two_days_ahead_offers_deposit_or_full(service):
    availability = service.check_availability(THURSDAY)
    assert availability.status is DateStatus.AVAILABLE
    assert availability.options == {PaymentChoice.DEPOSIT: DEPOSIT_AMOUNTS, PaymentChoice.FULL: FULL_AMOUNTS}


def test_pending_booking_does_not_block_the_date(service):
    book(service)
    assert service.check_availability(THURSDAY).status is DateStatus.AVAILABLE


def test_confirmed_booking_blocks_the_date(service, db):
    booking = book(service)
    confirm_directly(db, booking.reference)
    availability = service.check_availability(THURSDAY)
    assert availability.status is DateStatus.TAKEN
    assert availability.options == {}


def test_confirmed_booking_blocks_only_its_own_date(service, db):
    confirm_directly(db, book(service).reference)
    assert service.check_availability(date(2026, 10, 2)).status is DateStatus.AVAILABLE


# --- Creating a booking -------------------------------------------------------------

def test_creates_a_pending_booking_with_everything_stored(service):
    booking = book(service, customer_name="  Ahmad   Al-Kandari ", customer_phone="+965 9999 9999")
    assert booking.reference == "CS-0001"
    assert booking.booking_date == THURSDAY
    assert booking.location_id == "julaia"
    assert booking.customer_name == "Ahmad Al-Kandari"
    assert booking.customer_phone == "+96599999999"
    assert booking.payment_choice is PaymentChoice.DEPOSIT
    assert booking.amounts == DEPOSIT_AMOUNTS
    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert booking.date_conflict is False
    assert booking.payment_proof is None
    assert booking.created_at == NOW


def test_full_payment_booking(service):
    assert book(service, payment_choice="full").amounts == FULL_AMOUNTS


def test_references_count_up(service):
    references = [book(service, channel_user_id=f"customer-{n}").reference for n in range(3)]
    assert references == ["CS-0001", "CS-0002", "CS-0003"]


def test_creation_is_recorded_in_the_history(service, db):
    booking = book(service)
    event = db.execute("SELECT * FROM booking_events WHERE booking_id = ?", (booking.id,)).fetchone()
    assert (event["actor"], event["event"], event["new_status"]) == ("ai", "created", "pending_payment")


def test_amounts_do_not_change_when_the_price_changes_later(service, db):
    booking = book(service)
    from dataclasses import replace
    pricier = replace(INFO, pricing=replace(INFO.pricing, base_price=70))
    later_service = BookingService(db, pricier, clock=lambda: NOW)
    assert later_service.get_booking(booking.reference).amounts == DEPOSIT_AMOUNTS


def test_get_booking_ignores_case_and_spaces(service):
    book(service)
    assert service.get_booking(" cs-0001 ").reference == "CS-0001"
    assert service.get_booking("CS-9999") is None


# --- Creating a booking: every rule is checked again --------------------------------

@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"booking_date": SUNDAY}, RefusalReason.PAST_DATE),
        ({"booking_date": MONDAY}, RefusalReason.SAME_DAY),
        ({"location_id": "arifjan"}, RefusalReason.UNKNOWN_LOCATION),   # alias, not an id
        ({"location_id": "kabd"}, RefusalReason.UNKNOWN_LOCATION),
        ({"customer_name": "   "}, RefusalReason.INVALID_NAME),
        ({"customer_name": "A" * 101}, RefusalReason.INVALID_NAME),
        ({"customer_phone": "12345"}, RefusalReason.INVALID_PHONE),
        ({"customer_phone": "not a number"}, RefusalReason.INVALID_PHONE),
        ({"booking_date": TUESDAY, "payment_choice": "deposit"}, RefusalReason.PAYMENT_CHOICE_NOT_ALLOWED),
        ({"payment_choice": "half"}, RefusalReason.PAYMENT_CHOICE_NOT_ALLOWED),
    ],
)
def test_refuses_booking_that_breaks_a_rule(service, db, changes, reason):
    with pytest.raises(BookingRefused) as refused:
        book(service, **changes)
    assert refused.value.reason is reason
    # Nothing was saved.
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0


def test_refuses_a_date_that_is_already_confirmed(service, db):
    confirm_directly(db, book(service).reference)
    with pytest.raises(BookingRefused) as refused:
        book(service)
    assert refused.value.reason is RefusalReason.DATE_TAKEN


def test_many_pending_bookings_may_share_a_date(service):
    book(service, customer_name="First")
    book(service, customer_name="Second")
    assert service.get_booking("CS-0002").status is BookingStatus.PENDING_PAYMENT


def test_a_failed_booking_leaves_no_trace(service, db):
    with pytest.raises(BookingRefused):
        book(service, booking_date=MONDAY)
    assert db.execute("SELECT COUNT(*) FROM booking_events").fetchone()[0] == 0
    assert not db.in_transaction


# --- Phone numbers and references --------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("99999999", "+96599999999"),
        ("9999 9999", "+96599999999"),
        ("+965 9999 9999", "+96599999999"),
        ("0096599999999", "+96599999999"),
        ("٩٩٩٩٩٩٩٩", "+96599999999"),               # Arabic digits
        ("+966 50 123 4567", "+966501234567"),       # Saudi Arabia
        ("+44 7911 123456", "+447911123456"),        # United Kingdom
        ("12345", None),
        ("", None),
        ("hello", None),
    ],
)
def test_normalize_phone(raw, normalized):
    assert normalize_phone(raw) == normalized


def test_format_reference():
    assert format_reference(1) == "CS-0001"
    assert format_reference(42) == "CS-0042"
    assert format_reference(12345) == "CS-12345"


# --- Payment screenshots (Piece 2.4) --------------------------------------------------

def events(db, booking, *, with_outbox=False):
    """The booking's history. The outbox's own entries are left out unless asked for."""
    rows = [dict(row) for row in db.execute(
        "SELECT actor, event, old_status, new_status FROM booking_events WHERE booking_id = ? ORDER BY id",
        (booking.id,))]
    return rows if with_outbox else [row for row in rows if row["event"] != "outbox"]


def test_screenshot_moves_booking_to_payment_submitted(service, db):
    booking = book(service)
    updated = service.attach_payment_proof("CS-0001", "99999999", JPG)
    assert updated.status is BookingStatus.PAYMENT_SUBMITTED
    assert updated.payment_proof == "CS-0001-1.jpg"
    assert (service.proofs_dir / "CS-0001-1.jpg").read_bytes() == JPG
    assert events(db, booking)[-1] == {
        "actor": "ai", "event": "status_changed", "old_status": "pending_payment", "new_status": "payment_submitted",
    }


def test_screenshot_is_not_confirmation_and_does_not_block_the_date(service):
    book(service)
    service.attach_payment_proof("CS-0001", "99999999", JPG)
    assert service.check_availability(THURSDAY).status is DateStatus.AVAILABLE


def test_a_second_screenshot_is_kept_too(service, db):
    booking = book(service)
    service.attach_payment_proof("CS-0001", "99999999", JPG)
    updated = service.attach_payment_proof("CS-0001", "99999999", PNG)
    assert updated.status is BookingStatus.PAYMENT_SUBMITTED
    assert updated.payment_proof == "CS-0001-2.png"
    assert sorted(path.name for path in service.proofs_dir.iterdir()) == ["CS-0001-1.jpg", "CS-0001-2.png"]
    assert events(db, booking)[-1]["event"] == "payment_proof_replaced"


@pytest.mark.parametrize(("reference", "phone"), [("CS-0001", "55555555"), ("CS-0009", "99999999")])
def test_screenshot_needs_matching_reference_and_phone(service, reference, phone):
    book(service)
    with pytest.raises(BookingRefused) as refused:
        service.attach_payment_proof(reference, phone, JPG)
    assert refused.value.reason is RefusalReason.BOOKING_NOT_FOUND
    assert not service.proofs_dir.exists()


@pytest.mark.parametrize("status", ["confirmed", "completed", "cancelled"])
def test_screenshot_refused_once_booking_is_past_payment(service, db, status):
    book(service)
    set_status_directly(db, "CS-0001", status)
    with pytest.raises(BookingRefused) as refused:
        service.attach_payment_proof("CS-0001", "99999999", JPG)
    assert refused.value.reason is RefusalReason.WRONG_STATUS


@pytest.mark.parametrize("data", [PROGRAM, b"", JPG + b"\x00" * MAX_PROOF_BYTES], ids=["program", "empty", "too big"])
def test_screenshot_refused_if_not_an_accepted_file(service, data):
    book(service)
    with pytest.raises(BookingRefused) as refused:
        service.attach_payment_proof("CS-0001", "99999999", data)
    assert refused.value.reason is RefusalReason.INVALID_FILE
    assert service.get_booking("CS-0001").status is BookingStatus.PENDING_PAYMENT
    assert not service.proofs_dir.exists()


@pytest.mark.parametrize(
    ("data", "file_type"),
    [(JPG, "jpg"), (PNG, "png"), (PDF, "pdf"), (WEBP, "webp"), (HEIC, "heic"), (PROGRAM, None), (b"", None)],
)
def test_detect_proof_file_type(data, file_type):
    assert detect_proof_file_type(data) == file_type


# --- Customer status lookup -----------------------------------------------------------

def test_customer_finds_their_booking_with_any_phone_format(service):
    book(service, customer_phone="99999999")
    assert service.find_customer_booking("cs-0001", "+965 9999 9999").reference == "CS-0001"
    assert service.find_customer_booking("CS-0001", "٩٩٩٩٩٩٩٩").reference == "CS-0001"


@pytest.mark.parametrize(
    ("reference", "phone"),
    [("CS-0001", "55555555"), ("CS-0002", "99999999"), ("CS-0001", "not a phone"), ("CS-0001", "")],
)
def test_customer_lookup_reveals_nothing_without_both_matching(service, reference, phone):
    book(service)
    assert service.find_customer_booking(reference, phone) is None


def test_customer_lookup_shows_the_date_conflict_flag(service, db):
    book(service)
    with db:
        db.execute("UPDATE bookings SET date_conflict = 1 WHERE reference = 'CS-0001'")
    assert service.find_customer_booking("CS-0001", "99999999").date_conflict is True


# --- Handoffs -------------------------------------------------------------------------

def test_handoff_is_added_to_the_owners_list(service):
    handoff = service.create_handoff(
        HandoffType.SAME_DAY, "Wants a setup tonight at Julaia",
        customer_name="Ahmad", customer_phone="9999 9999", channel="terminal", channel_user_id="test",
    )
    assert handoff.type is HandoffType.SAME_DAY
    assert handoff.status is HandoffStatus.OPEN
    assert handoff.customer_phone == "+96599999999"
    assert handoff.created_at == NOW
    assert handoff.booking_id is None


def test_handoff_about_a_booking_is_linked_and_recorded_in_its_history(service, db):
    booking = book(service)
    handoff = service.create_handoff("cancellation", "Wants to cancel", booking_reference="cs-0001")
    assert handoff.booking_id == booking.id
    assert events(db, booking)[-1]["event"] == "handed_off"


def test_handoff_never_fails_because_of_what_the_customer_typed(service):
    handoff = service.create_handoff(
        HandoffType.OTHER, "   ", customer_phone="call me on 123", booking_reference="CS-0777",
    )
    assert handoff.customer_phone == "call me on 123"
    assert "(no summary given)" in handoff.summary
    assert "'CS-0777', which was not found" in handoff.summary
    assert handoff.booking_id is None


def test_handoff_with_an_unknown_type_is_a_bug_in_our_code(service):
    with pytest.raises(ValueError):
        service.create_handoff("complaint", "x")


# --- Owner actions (Piece 2.5) ------------------------------------------------------------

def test_approving_confirms_and_blocks_the_date(service, db):
    booking = book(service)
    service.attach_payment_proof("CS-0001", "99999999", JPG)
    approval = service.approve_payment("CS-0001", "25 KWD received")
    assert approval.booking.status is BookingStatus.CONFIRMED
    assert approval.conflicts == []
    assert service.check_availability(THURSDAY).status is DateStatus.TAKEN
    assert events(db, booking)[-1] == {
        "actor": "owner", "event": "status_changed", "old_status": "payment_submitted", "new_status": "confirmed",
    }


def test_owner_can_approve_without_a_screenshot(service):
    book(service)
    assert service.approve_payment("CS-0001").booking.status is BookingStatus.CONFIRMED


def test_approving_flags_every_other_waiting_booking_for_that_date(service):
    book(service, customer_name="First")                        # CS-0001
    book(service, customer_name="Second")                       # CS-0002
    service.attach_payment_proof("CS-0002", "99999999", JPG)
    book(service, customer_name="Other day", booking_date=date(2026, 10, 2))  # CS-0003

    approval = service.approve_payment("CS-0001")

    assert [b.reference for b in approval.conflicts] == ["CS-0002"]
    assert service.get_booking("CS-0002").date_conflict is True
    assert service.get_booking("CS-0002").status is BookingStatus.PAYMENT_SUBMITTED  # nothing decided for them
    assert service.get_booking("CS-0003").date_conflict is False

    [handoff] = service.list_handoffs()
    assert handoff.type is HandoffType.DATE_CONFLICT
    assert handoff.booking_id == service.get_booking("CS-0002").id
    assert "CS-0001 was confirmed" in handoff.summary
    assert "sent a payment screenshot" in handoff.summary
    assert "has not been told" in handoff.summary


def test_the_flagged_customer_sees_the_flag_in_their_status_lookup(service):
    book(service, customer_name="First")
    book(service, customer_name="Second", customer_phone="55555555")
    service.approve_payment("CS-0001")
    assert service.find_customer_booking("CS-0002", "55555555").date_conflict is True


def test_cannot_approve_a_second_booking_for_a_confirmed_date(service):
    book(service, customer_name="First")
    book(service, customer_name="Second")
    service.approve_payment("CS-0001")
    with pytest.raises(BookingRefused) as refused:
        service.approve_payment("CS-0002")
    assert refused.value.reason is RefusalReason.DATE_TAKEN


def test_after_a_cancellation_the_owner_may_approve_a_flagged_booking(service):
    book(service, customer_name="First")
    book(service, customer_name="Second")
    service.approve_payment("CS-0001")
    service.cancel_booking("CS-0001", "customer cancelled in time, refunded")
    approval = service.approve_payment("CS-0002")
    assert approval.booking.status is BookingStatus.CONFIRMED
    assert approval.booking.date_conflict is False   # the owner has decided


@pytest.mark.parametrize("status", ["confirmed", "completed", "cancelled"])
def test_cannot_approve_twice_or_after_the_end(service, db, status):
    book(service)
    set_status_directly(db, "CS-0001", status)
    with pytest.raises(BookingRefused) as refused:
        service.approve_payment("CS-0001")
    assert refused.value.reason is RefusalReason.WRONG_STATUS


def test_cannot_approve_a_booking_whose_date_has_passed(db, tmp_path):
    early = BookingService(db, INFO, clock=lambda: NOW, proofs_dir=tmp_path)
    book(early, booking_date=TUESDAY, payment_choice="full")
    later = BookingService(db, INFO, clock=lambda: datetime(2026, 9, 30, 12, 0, tzinfo=INFO.timezone))
    with pytest.raises(BookingRefused) as refused:
        later.approve_payment("CS-0001")
    assert refused.value.reason is RefusalReason.PAST_DATE


def test_unknown_reference(service):
    with pytest.raises(BookingRefused) as refused:
        service.approve_payment("CS-0404")
    assert refused.value.reason is RefusalReason.BOOKING_NOT_FOUND


def test_rejecting_a_screenshot_goes_back_to_pending_payment(service):
    book(service)
    service.attach_payment_proof("CS-0001", "99999999", JPG)
    booking = service.reject_payment("CS-0001", "no transfer in the account")
    assert booking.status is BookingStatus.PENDING_PAYMENT
    # The customer can send a new screenshot.
    assert service.attach_payment_proof("CS-0001", "99999999", PNG).status is BookingStatus.PAYMENT_SUBMITTED


def test_can_only_reject_when_a_screenshot_was_sent(service):
    book(service)
    with pytest.raises(BookingRefused) as refused:
        service.reject_payment("CS-0001")
    assert refused.value.reason is RefusalReason.WRONG_STATUS


@pytest.mark.parametrize("before", ["pending", "submitted", "confirmed"])
def test_cancelling_frees_the_date(service, before):
    book(service)
    if before == "submitted":
        service.attach_payment_proof("CS-0001", "99999999", JPG)
    if before == "confirmed":
        service.approve_payment("CS-0001")
    assert service.cancel_booking("CS-0001").status is BookingStatus.CANCELLED
    assert service.check_availability(THURSDAY).status is DateStatus.AVAILABLE


def test_a_cancelled_booking_stays_cancelled(service):
    book(service)
    service.cancel_booking("CS-0001")
    for action in (service.cancel_booking, service.approve_payment, service.complete_booking):
        with pytest.raises(BookingRefused):
            action("CS-0001")


def test_completing_only_confirmed_bookings_on_or_after_the_day(db, tmp_path):
    early = BookingService(db, INFO, clock=lambda: NOW, proofs_dir=tmp_path)
    book(early)
    early.approve_payment("CS-0001")
    with pytest.raises(BookingRefused) as refused:
        early.complete_booking("CS-0001")
    assert refused.value.reason is RefusalReason.TOO_EARLY

    on_the_day = BookingService(db, INFO, clock=lambda: datetime(2026, 10, 1, 23, 30, tzinfo=INFO.timezone))
    assert on_the_day.complete_booking("CS-0001").status is BookingStatus.COMPLETED
    assert on_the_day.check_availability(date(2026, 10, 2)).status is DateStatus.AVAILABLE


def test_rescheduling_keeps_status_and_amounts(service, db):
    booking = book(service)
    service.approve_payment("CS-0001")
    moved = service.reschedule_booking("CS-0001", date(2026, 10, 8), "bad weather, customer postponed").booking
    assert moved.booking_date == date(2026, 10, 8)
    assert moved.status is BookingStatus.CONFIRMED
    assert moved.amounts == DEPOSIT_AMOUNTS
    assert service.check_availability(THURSDAY).status is DateStatus.AVAILABLE      # old date freed
    assert service.check_availability(date(2026, 10, 8)).status is DateStatus.TAKEN # new date blocked
    assert events(db, booking)[-1]["event"] == "rescheduled"


def test_rescheduling_a_confirmed_booking_flags_waiting_bookings_on_the_new_date(service):
    book(service)                                                         # CS-0001, Thursday
    book(service, booking_date=date(2026, 10, 8), customer_name="Waiting")  # CS-0002
    service.approve_payment("CS-0001")
    result = service.reschedule_booking("CS-0001", date(2026, 10, 8))
    assert [b.reference for b in result.conflicts] == ["CS-0002"]
    assert service.list_handoffs()[0].type is HandoffType.DATE_CONFLICT


def test_rescheduling_a_flagged_booking_to_a_free_date_clears_the_flag(service):
    book(service, customer_name="First")
    book(service, customer_name="Second")
    service.approve_payment("CS-0001")
    moved = service.reschedule_booking("CS-0002", date(2026, 10, 8)).booking
    assert moved.date_conflict is False
    assert moved.status is BookingStatus.PENDING_PAYMENT


def test_owner_may_reschedule_to_today(service):
    book(service)
    assert service.reschedule_booking("CS-0001", MONDAY).booking.booking_date == MONDAY


@pytest.mark.parametrize(
    ("new_date", "reason"),
    [(SUNDAY, RefusalReason.PAST_DATE), (THURSDAY, RefusalReason.WRONG_STATUS),
     (date(2026, 10, 8), RefusalReason.DATE_TAKEN)],
)
def test_rescheduling_refused(service, new_date, reason):
    book(service)                                                     # CS-0001, Thursday
    book(service, booking_date=date(2026, 10, 8), customer_name="X")  # CS-0002
    service.approve_payment("CS-0002")
    with pytest.raises(BookingRefused) as refused:
        service.reschedule_booking("CS-0001", new_date)
    assert refused.value.reason is reason


def test_list_bookings(service):
    book(service, booking_date=date(2026, 10, 8))
    book(service, channel_user_id="customer-2")
    book(service, channel_user_id="customer-3")
    service.approve_payment("CS-0002")
    assert [b.reference for b in service.list_bookings()] == ["CS-0002", "CS-0003", "CS-0001"]
    assert [b.reference for b in service.list_bookings(statuses=(BookingStatus.CONFIRMED,))] == ["CS-0002"]
    assert [b.reference for b in service.list_bookings(on_date=THURSDAY)] == ["CS-0002", "CS-0003"]


def test_booking_history_tells_the_whole_story(service):
    book(service)
    service.attach_payment_proof("CS-0001", "99999999", JPG)
    service.approve_payment("CS-0001", "received")
    story = [(row["actor"], row["event"], row["new_status"])
             for row in service.booking_history("CS-0001") if row["event"] != "outbox"]
    assert story == [
        ("ai", "created", "pending_payment"),
        ("ai", "status_changed", "payment_submitted"),
        ("owner", "status_changed", "confirmed"),
    ]


def test_resolving_a_handoff(service):
    handoff = service.create_handoff(HandoffType.WEATHER, "Asks if it will rain")
    resolved = service.resolve_handoff(handoff.id, "Called the customer; they postponed")
    assert resolved.status is HandoffStatus.RESOLVED
    assert resolved.resolved_at == NOW
    assert resolved.resolution_note == "Called the customer; they postponed"
    assert service.list_handoffs() == []
    assert service.list_handoffs(status=None) == [resolved]
    with pytest.raises(BookingRefused):
        service.resolve_handoff(handoff.id)
    with pytest.raises(BookingRefused) as refused:
        service.resolve_handoff(999)
    assert refused.value.reason is RefusalReason.HANDOFF_NOT_FOUND


def test_every_owner_action_leaves_no_open_transaction_even_when_refused(service, db):
    with pytest.raises(BookingRefused):
        service.approve_payment("CS-0404")
    assert not db.in_transaction


# --- Owner-created bookings (Piece 3.1) ----------------------------------------------------

def owner_book(service, **changes):
    request = {
        "booking_date": THURSDAY, "location_id": "julaia", "customer_name": "Ahmad",
        "customer_phone": "99999999", "payment_choice": "full",
    }
    request.update(changes)
    return service.create_owner_booking(**request)


def test_owner_booking_starts_as_pending_payment_by_default(service, db):
    booking = owner_book(service).booking
    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert booking.channel == "owner"
    assert service.check_availability(THURSDAY).status is DateStatus.AVAILABLE  # not blocked yet
    assert events(db, booking)[0]["actor"] == "owner"


def test_owner_booking_marked_paid_is_confirmed_immediately(service, db):
    booking = owner_book(service, paid=True).booking
    assert booking.status is BookingStatus.CONFIRMED
    assert service.check_availability(THURSDAY).status is DateStatus.TAKEN
    assert [e["new_status"] for e in events(db, booking)] == ["pending_payment", "confirmed"]


def test_owner_may_book_today_with_full_payment(service):
    booking = owner_book(service, booking_date=MONDAY, paid=True).booking
    assert booking.booking_date == MONDAY
    assert booking.amounts == FULL_AMOUNTS


def test_ai_still_cannot_book_today(service):
    with pytest.raises(BookingRefused) as refused:
        book(service, booking_date=MONDAY, payment_choice="full")
    assert refused.value.reason is RefusalReason.SAME_DAY
    assert service.check_availability(MONDAY).status is DateStatus.SAME_DAY


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"booking_date": MONDAY, "payment_choice": "deposit"}, RefusalReason.PAYMENT_CHOICE_NOT_ALLOWED),
        ({"booking_date": TUESDAY, "payment_choice": "deposit"}, RefusalReason.PAYMENT_CHOICE_NOT_ALLOWED),
        ({"booking_date": SUNDAY}, RefusalReason.PAST_DATE),
        ({"location_id": "kabd"}, RefusalReason.UNKNOWN_LOCATION),
        ({"customer_phone": "123"}, RefusalReason.INVALID_PHONE),
        ({"customer_name": ""}, RefusalReason.INVALID_NAME),
    ],
)
def test_paid_does_not_bypass_validation(service, db, changes, reason):
    with pytest.raises(BookingRefused) as refused:
        owner_book(service, paid=True, **changes)
    assert refused.value.reason is reason
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0


def test_owner_may_choose_deposit_two_or_more_days_ahead(service):
    booking = owner_book(service, payment_choice="deposit").booking
    assert booking.amounts == DEPOSIT_AMOUNTS


def test_paid_does_not_bypass_date_conflict_protection(service, db):
    owner_book(service, paid=True)
    with pytest.raises(BookingRefused) as refused:
        owner_book(service, customer_name="Second", paid=True)
    assert refused.value.reason is RefusalReason.DATE_TAKEN
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1


def test_paid_owner_booking_flags_waiting_bookings_like_an_approval(service):
    book(service, customer_name="Waiting")          # CS-0001, pending, from the AI
    result = owner_book(service, paid=True)         # CS-0002, confirmed
    assert [b.reference for b in result.conflicts] == ["CS-0001"]
    assert service.get_booking("CS-0001").date_conflict is True
    assert service.list_handoffs()[0].type is HandoffType.DATE_CONFLICT


def test_unpaid_owner_booking_is_approved_like_any_other(service):
    owner_book(service)
    assert service.approve_payment("CS-0001", "cash").booking.status is BookingStatus.CONFIRMED


# --- The same booking requested twice (Step 7.6) ---------------------------------------------

def test_the_same_request_again_returns_the_waiting_booking_instead_of_a_second_one(service, db):
    first = book(service)
    again = book(service, customer_phone="+965 9999 9999")      # the same phone, written differently
    assert again.reference == first.reference
    assert db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
    events = [row["event"] for row in db.execute("SELECT event FROM booking_events ORDER BY id")]
    assert events == ["created", "duplicate_request"]


def test_it_also_returns_the_booking_once_a_screenshot_was_sent(service):
    first = book(service)
    service.attach_payment_proof(first.reference, "99999999", JPG)
    assert book(service).reference == first.reference


@pytest.mark.parametrize("changes", [
    {"channel_user_id": "someone-else"}, {"booking_date": date(2026, 10, 8)}, {"location_id": "bnaider"},
    {"customer_name": "Ahmad Ali"}, {"customer_phone": "66666666"}, {"payment_choice": "full"},
], ids=["other customer", "other date", "other location", "other name", "other phone", "other payment"])
def test_any_different_detail_makes_a_new_booking(service, changes):
    first = book(service)
    assert book(service, **changes).reference != first.reference


def test_a_cancelled_booking_is_not_reused(service):
    first = book(service)
    service.cancel_booking(first.reference)
    assert book(service).reference != first.reference


def test_owner_bookings_are_never_merged(service):
    request = dict(booking_date=THURSDAY, location_id="julaia", customer_name="Mona", customer_phone="66666666",
                   payment_choice="full", paid=False)
    first = service.create_owner_booking(**request).booking
    second = service.create_owner_booking(**request).booking
    assert first.reference != second.reference
