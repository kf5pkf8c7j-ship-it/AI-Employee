"""The booking service: every booking rule, applied to the database.

The AI's tools (Step 4) and the owner's commands (Step 3) both go through
BookingService, so the same rules apply whoever is acting.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path

import phonenumbers

from cozysetup.business_info import BusinessInfo
from cozysetup import outbox
from cozysetup.database import DEFAULT_DB_PATH, Actor, BookingStatus, HandoffStatus, HandoffType, Language, OutboxKind
from cozysetup.rules import (
    Amounts,
    DateWindow,
    PaymentChoice,
    calculate_amounts,
    date_window,
    payment_options,
)

# Phone numbers written without a country code are read as Kuwaiti.
DEFAULT_PHONE_REGION = "KW"
MAX_NAME_LENGTH = 100

# The channel recorded for bookings the owner enters: the owner is in touch
# with the customer directly.
OWNER_CHANNEL = outbox.OWNER_CHANNEL

# Statuses that block the date for everyone else.
BLOCKING_STATUSES = (BookingStatus.CONFIRMED, BookingStatus.COMPLETED)

# Which status may change to which. Anything not listed is refused.
ALLOWED_STATUS_CHANGES = {
    BookingStatus.PENDING_PAYMENT: {BookingStatus.PAYMENT_SUBMITTED, BookingStatus.CONFIRMED, BookingStatus.CANCELLED},
    BookingStatus.PAYMENT_SUBMITTED: {BookingStatus.PENDING_PAYMENT, BookingStatus.CONFIRMED, BookingStatus.CANCELLED},
    BookingStatus.CONFIRMED: {BookingStatus.COMPLETED, BookingStatus.CANCELLED},
    BookingStatus.COMPLETED: set(),
    BookingStatus.CANCELLED: set(),
}

# Payment screenshots are kept next to the database, outside git.
DEFAULT_PROOFS_DIR = DEFAULT_DB_PATH.parent / "payment_proofs"
MAX_PROOF_BYTES = 10 * 1024 * 1024  # 10 MB



class DateStatus(StrEnum):
    PAST = "past"
    SAME_DAY = "same_day"      # handed to the owner
    TAKEN = "taken"            # another booking is confirmed for that date
    AVAILABLE = "available"


class RefusalReason(StrEnum):
    PAST_DATE = "past_date"
    SAME_DAY = "same_day"
    DATE_TAKEN = "date_taken"
    UNKNOWN_LOCATION = "unknown_location"
    INVALID_NAME = "invalid_name"
    INVALID_PHONE = "invalid_phone"
    PAYMENT_CHOICE_NOT_ALLOWED = "payment_choice_not_allowed"
    BOOKING_NOT_FOUND = "booking_not_found"
    WRONG_STATUS = "wrong_status"
    INVALID_FILE = "invalid_file"
    INVALID_LANGUAGE = "invalid_language"
    TOO_EARLY = "too_early"
    HANDOFF_NOT_FOUND = "handoff_not_found"


class BookingRefused(Exception):
    """A request broke a booking rule. `reason` tells the caller which one."""

    def __init__(self, reason: RefusalReason, message: str):
        self.reason = reason
        super().__init__(message)


@dataclass(frozen=True)
class Availability:
    booking_date: date
    status: DateStatus
    # The payment choices the customer may pick, with their amounts.
    # Empty unless the date is AVAILABLE.
    options: dict[PaymentChoice, Amounts]


@dataclass(frozen=True)
class Booking:
    id: int
    reference: str
    booking_date: date
    location_id: str
    customer_name: str
    customer_phone: str
    channel: str
    channel_user_id: str
    payment_choice: PaymentChoice
    amounts: Amounts
    status: BookingStatus
    date_conflict: bool
    payment_proof: str | None
    created_at: datetime
    updated_at: datetime
    language: Language | None = None   # the customer's language; None = not recorded

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Booking:
        return cls(
            id=row["id"],
            reference=row["reference"],
            booking_date=date.fromisoformat(row["booking_date"]),
            location_id=row["location_id"],
            customer_name=row["customer_name"],
            customer_phone=row["customer_phone"],
            channel=row["channel"],
            channel_user_id=row["channel_user_id"],
            payment_choice=PaymentChoice(row["payment_choice"]),
            amounts=Amounts(
                rental_price=row["rental_price"],
                amount_now=row["amount_now"],
                remaining_on_day=row["remaining_on_day"],
                security_deposit=row["security_deposit"],
            ),
            status=BookingStatus(row["status"]),
            date_conflict=bool(row["date_conflict"]),
            payment_proof=row["payment_proof"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            language=Language(row["language"]) if row["language"] else None,
        )


@dataclass(frozen=True)
class BookingPreview:
    """A booking request that passed every rule - not saved."""
    booking_date: date
    location_id: str
    customer_name: str    # tidied
    customer_phone: str   # international format
    payment_choice: PaymentChoice
    amounts: Amounts


@dataclass(frozen=True)
class Approval:
    """The result of confirming a booking on a date (approval or rescheduling)."""
    booking: Booking
    # Other waiting bookings for that date, now flagged and handed to the owner.
    conflicts: list[Booking]


@dataclass(frozen=True)
class Handoff:
    id: int
    created_at: datetime
    type: HandoffType
    summary: str
    customer_name: str | None
    customer_phone: str | None
    channel: str | None
    channel_user_id: str | None
    booking_id: int | None
    status: HandoffStatus
    resolved_at: datetime | None
    resolution_note: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Handoff:
        return cls(
            id=row["id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            type=HandoffType(row["type"]),
            summary=row["summary"],
            customer_name=row["customer_name"],
            customer_phone=row["customer_phone"],
            channel=row["channel"],
            channel_user_id=row["channel_user_id"],
            booking_id=row["booking_id"],
            status=HandoffStatus(row["status"]),
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            resolution_note=row["resolution_note"],
        )


def normalize_phone(raw: str) -> str | None:
    """Any way of writing a phone number -> international format, or None if invalid.

    "9999 9999", "+965 99999999", "0096599999999", "٩٩٩٩٩٩٩٩" -> "+96599999999"
    "+966 50 123 4567" -> "+966501234567"
    """
    try:
        number = phonenumbers.parse(raw, DEFAULT_PHONE_REGION)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def detect_proof_file_type(data: bytes) -> str | None:
    """The file type of a payment screenshot, recognised by its first bytes -
    not by its name, since a file called "receipt.jpg" could be anything.
    Returns the file extension, or None if it is not an accepted type."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):  # iPhone photos
        return "heic"
    return None


def format_reference(booking_id: int) -> str:
    return f"CS-{booking_id:04d}"


class BookingService:
    def __init__(
        self,
        db: sqlite3.Connection,
        info: BusinessInfo,
        clock: Callable[[], datetime] | None = None,
        proofs_dir: Path = DEFAULT_PROOFS_DIR,
    ):
        """`clock` returns the current time. Tests pass a fixed one; normally it's the real time.
        `proofs_dir` is where payment screenshots are saved."""
        self.db = db
        self.info = info
        self._clock = clock or (lambda: datetime.now(info.timezone))
        self.proofs_dir = proofs_dir

    # --- Time ---------------------------------------------------------------------

    def now(self) -> datetime:
        """The current time in the business's timezone."""
        return self._clock().astimezone(self.info.timezone)

    def today(self) -> date:
        return self.now().date()

    # --- Availability ---------------------------------------------------------------

    def check_availability(self, booking_date: date, *, for_owner: bool = False) -> Availability:
        """Whether a date can be booked, and the payment choices for it.

        Today is SAME_DAY (handed to the owner) - except for the owner, who may book it.
        """
        window = date_window(booking_date, self.today())
        if window is DateWindow.PAST:
            return Availability(booking_date, DateStatus.PAST, {})
        if window is DateWindow.SAME_DAY and not for_owner:
            return Availability(booking_date, DateStatus.SAME_DAY, {})
        if self._date_is_blocked(booking_date):
            return Availability(booking_date, DateStatus.TAKEN, {})
        options = {
            choice: calculate_amounts(choice, self.info.pricing, self.info.payment)
            for choice in payment_options(booking_date, self.today(), self.info.payment)
        }
        return Availability(booking_date, DateStatus.AVAILABLE, options)

    # --- Creating a booking -----------------------------------------------------------

    def create_booking(
        self,
        *,
        booking_date: date,
        location_id: str,
        customer_name: str,
        customer_phone: str,
        payment_choice: str,
        channel: str,
        channel_user_id: str,
        language: str | None = None,
    ) -> Booking:
        """The AI creates a PENDING_PAYMENT booking for a customer. Every rule is
        checked again here, whatever was checked earlier in the conversation.
        Same-day bookings are refused - they are handed to the owner.
        `language` is the language the customer writes in: en, ar or arabizi."""
        booking_id, _ = self._create_booking(
            booking_date=booking_date, location_id=location_id, customer_name=customer_name,
            customer_phone=customer_phone, payment_choice=payment_choice,
            channel=channel, channel_user_id=channel_user_id, actor=Actor.AI, paid=False,
            language=language,
        )
        return self._get_booking_by_id(booking_id)

    def preview_booking(
        self,
        *,
        booking_date: date,
        location_id: str,
        customer_name: str,
        customer_phone: str,
        payment_choice: str,
    ) -> BookingPreview:
        """Check a customer's booking request with every rule create_booking uses,
        without saving anything. Used to show the customer a summary first."""
        return self._check_request(
            booking_date=booking_date, location_id=location_id, customer_name=customer_name,
            customer_phone=customer_phone, payment_choice=payment_choice, for_owner=False,
        )

    def _check_request(
        self,
        *,
        booking_date: date,
        location_id: str,
        customer_name: str,
        customer_phone: str,
        payment_choice: str,
        for_owner: bool,
    ) -> BookingPreview:
        if location_id not in {location.id for location in self.info.locations}:
            raise BookingRefused(RefusalReason.UNKNOWN_LOCATION, f"unknown location id {location_id!r}")

        name = " ".join(customer_name.split())
        if not name or len(name) > MAX_NAME_LENGTH:
            raise BookingRefused(RefusalReason.INVALID_NAME, f"name must be 1-{MAX_NAME_LENGTH} characters")

        phone = normalize_phone(customer_phone)
        if phone is None:
            raise BookingRefused(RefusalReason.INVALID_PHONE, f"{customer_phone!r} is not a valid phone number")

        availability = self.check_availability(booking_date, for_owner=for_owner)
        if availability.status is not DateStatus.AVAILABLE:
            reason = {
                DateStatus.PAST: RefusalReason.PAST_DATE,
                DateStatus.SAME_DAY: RefusalReason.SAME_DAY,
                DateStatus.TAKEN: RefusalReason.DATE_TAKEN,
            }[availability.status]
            raise BookingRefused(reason, f"{booking_date} is not available ({availability.status})")

        try:
            choice = PaymentChoice(payment_choice)
        except ValueError:
            choice = None
        if choice not in availability.options:
            allowed = ", ".join(availability.options)
            raise BookingRefused(
                RefusalReason.PAYMENT_CHOICE_NOT_ALLOWED,
                f"payment choice {payment_choice!r} is not allowed for {booking_date} (allowed: {allowed})",
            )
        return BookingPreview(booking_date, location_id, name, phone, choice, availability.options[choice])

    def create_owner_booking(
        self,
        *,
        booking_date: date,
        location_id: str,
        customer_name: str,
        customer_phone: str,
        payment_choice: str,
        paid: bool = False,
    ) -> Approval:
        """The owner creates a booking (e.g. taken by phone, or a same-day booking).

        Same rules as the AI's bookings, except that today is allowed (100% only).
        paid=True means the owner has personally verified or received the payment:
        the booking is confirmed straight away - with the same date checks and
        conflict flagging as approve_payment(). It never bypasses validation.
        """
        booking_id, conflicts = self._create_booking(
            booking_date=booking_date, location_id=location_id, customer_name=customer_name,
            customer_phone=customer_phone, payment_choice=payment_choice,
            channel=OWNER_CHANNEL, channel_user_id="", actor=Actor.OWNER, paid=paid,
        )
        return Approval(
            booking=self._get_booking_by_id(booking_id),
            conflicts=[self._get_booking_by_id(conflict.id) for conflict in conflicts],
        )

    def _create_booking(
        self,
        *,
        booking_date: date,
        location_id: str,
        customer_name: str,
        customer_phone: str,
        payment_choice: str,
        channel: str,
        channel_user_id: str,
        actor: Actor,
        paid: bool,
        language: str | None = None,
    ) -> tuple[int, list[Booking]]:
        try:
            customer_language = Language(language) if language else None
        except ValueError:
            raise BookingRefused(RefusalReason.INVALID_LANGUAGE,
                                 f"language must be en, ar or arabizi, not {language!r}") from None
        with self._write_transaction():
            # Checked inside the transaction, so nobody can confirm the date in between.
            preview = self._check_request(
                booking_date=booking_date, location_id=location_id, customer_name=customer_name,
                customer_phone=customer_phone, payment_choice=payment_choice, for_owner=actor is Actor.OWNER,
            )
            name, phone, choice, amounts = preview.customer_name, preview.customer_phone, preview.payment_choice, preview.amounts
            booking_id = self.db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM bookings").fetchone()[0]
            now = self._timestamp()
            self.db.execute(
                """
                INSERT INTO bookings (
                    id, reference, booking_date, location_id, customer_name, customer_phone,
                    channel, channel_user_id, payment_choice, rental_price, amount_now,
                    remaining_on_day, security_deposit, status, created_at, updated_at, language
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    booking_id, format_reference(booking_id), booking_date.isoformat(), location_id,
                    name, phone, channel, channel_user_id, choice.value, amounts.rental_price,
                    amounts.amount_now, amounts.remaining_on_day, amounts.security_deposit,
                    BookingStatus.PENDING_PAYMENT.value, now, now,
                    customer_language.value if customer_language else None,
                ),
            )
            self._record_event(
                booking_id, actor, "created",
                new_status=BookingStatus.PENDING_PAYMENT, details=f"payment choice: {choice.value}",
            )

            conflicts: list[Booking] = []
            if paid:
                booking = self._get_booking_by_id(booking_id)
                self._change_status(
                    booking, BookingStatus.CONFIRMED, Actor.OWNER, "created as paid: owner verified the payment"
                )
                conflicts = self._flag_conflicts(booking)
                self._queue_confirmation_and_reminder(booking_id)
        return booking_id, conflicts

    # --- Payment screenshots ------------------------------------------------------------

    def attach_payment_proof(self, reference: str, customer_phone: str, data: bytes) -> Booking:
        """Save a customer's payment screenshot.

        The booking moves to PAYMENT_SUBMITTED. That is NOT confirmation: only the
        owner confirms, after checking the money actually arrived. A second
        screenshot for the same booking is kept too, and becomes the current one.
        """
        booking = self.find_customer_booking(reference, customer_phone)
        if booking is None:
            raise BookingRefused(RefusalReason.BOOKING_NOT_FOUND, "no booking with that reference and phone number")
        self._require_proof_expected(booking)
        if not data or len(data) > MAX_PROOF_BYTES:
            raise BookingRefused(RefusalReason.INVALID_FILE, f"file must be 1 byte to {MAX_PROOF_BYTES} bytes")
        file_type = detect_proof_file_type(data)
        if file_type is None:
            raise BookingRefused(RefusalReason.INVALID_FILE, "file is not a JPG, PNG, WEBP, HEIC image or PDF")

        self.proofs_dir.mkdir(parents=True, exist_ok=True)
        path = self._next_proof_path(booking.reference, file_type)
        path.write_bytes(data)
        try:
            with self._write_transaction():
                # Read again inside the transaction: the owner may have changed it meanwhile.
                booking = self._get_booking_by_id(booking.id)
                self._require_proof_expected(booking)
                self.db.execute(
                    "UPDATE bookings SET payment_proof = ?, updated_at = ? WHERE id = ?",
                    (path.name, self._timestamp(), booking.id),
                )
                if booking.status is BookingStatus.PENDING_PAYMENT:
                    self._change_status(booking, BookingStatus.PAYMENT_SUBMITTED, Actor.AI, f"screenshot: {path.name}")
                else:
                    self._record_event(booking.id, Actor.AI, "payment_proof_replaced", details=f"screenshot: {path.name}")
        except BaseException:
            path.unlink(missing_ok=True)  # never keep a file the database doesn't know about
            raise
        return self._get_booking_by_id(booking.id)

    @staticmethod
    def _require_proof_expected(booking: Booking) -> None:
        if booking.status not in (BookingStatus.PENDING_PAYMENT, BookingStatus.PAYMENT_SUBMITTED):
            raise BookingRefused(
                RefusalReason.WRONG_STATUS, f"{booking.reference} is {booking.status} - a screenshot is not expected"
            )

    def _next_proof_path(self, reference: str, file_type: str) -> Path:
        number = 1
        while list(self.proofs_dir.glob(f"{reference}-{number}.*")):
            number += 1
        return self.proofs_dir / f"{reference}-{number}.{file_type}"

    # --- Customer status lookup -------------------------------------------------------

    def find_customer_booking(self, reference: str, customer_phone: str) -> Booking | None:
        """A customer's own booking. Both the reference AND the phone number must match,
        so guessing a reference like CS-0003 reveals nothing.

        Callers must check `date_conflict`: if set, the customer is only told
        the owner will contact them (reply "date_conflict_status").
        """
        booking = self.get_booking(reference)
        phone = normalize_phone(customer_phone)
        if booking is None or phone is None or booking.customer_phone != phone:
            return None
        return booking

    # --- Handoffs -----------------------------------------------------------------------

    def create_handoff(
        self,
        handoff_type: HandoffType | str,
        summary: str,
        *,
        customer_name: str | None = None,
        customer_phone: str | None = None,
        channel: str | None = None,
        channel_user_id: str | None = None,
        booking_reference: str | None = None,
        actor: Actor = Actor.AI,
    ) -> Handoff:
        """Add a case to the owner's to-do list.

        A handoff must never fail because of what the customer typed - it is the
        safety exit when anything is unclear. So details are kept as given when
        they can't be tidied: a phone number that doesn't validate is stored as
        typed, and an unknown booking reference is noted in the summary.
        """
        handoff_type = HandoffType(handoff_type)  # a wrong type is a bug in our code, not the customer's
        summary = summary.strip() or "(no summary given)"

        phone = None
        if customer_phone and customer_phone.strip():
            phone = normalize_phone(customer_phone) or customer_phone.strip()

        booking_id = None
        if booking_reference:
            booking = self.get_booking(booking_reference)
            if booking:
                booking_id = booking.id
            else:
                summary += f"\n(Customer mentioned booking {booking_reference!r}, which was not found.)"

        with self._write_transaction():
            handoff_id = self._insert_handoff(
                handoff_type, summary, actor, customer_name=customer_name, customer_phone=phone,
                channel=channel, channel_user_id=channel_user_id, booking_id=booking_id,
            )
        return self.get_handoff(handoff_id)

    def get_handoff(self, handoff_id: int) -> Handoff | None:
        row = self.db.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
        return Handoff.from_row(row) if row else None

    def list_handoffs(self, status: HandoffStatus | None = HandoffStatus.OPEN) -> list[Handoff]:
        """The owner's to-do list, oldest first. status=None lists all."""
        if status is None:
            rows = self.db.execute("SELECT * FROM handoffs ORDER BY id")
        else:
            rows = self.db.execute("SELECT * FROM handoffs WHERE status = ? ORDER BY id", (status.value,))
        return [Handoff.from_row(row) for row in rows]

    def resolve_handoff(self, handoff_id: int, note: str = "") -> Handoff:
        """Owner: mark a handoff as dealt with."""
        with self._write_transaction():
            handoff = self.get_handoff(handoff_id)
            if handoff is None:
                raise BookingRefused(RefusalReason.HANDOFF_NOT_FOUND, f"no handoff #{handoff_id}")
            if handoff.status is HandoffStatus.RESOLVED:
                raise BookingRefused(RefusalReason.WRONG_STATUS, f"handoff #{handoff_id} is already resolved")
            self.db.execute(
                "UPDATE handoffs SET status = ?, resolved_at = ?, resolution_note = ? WHERE id = ?",
                (HandoffStatus.RESOLVED.value, self._timestamp(), note.strip(), handoff_id),
            )
        return self.get_handoff(handoff_id)

    def _insert_handoff(
        self,
        handoff_type: HandoffType,
        summary: str,
        actor: Actor,
        *,
        customer_name: str | None = None,
        customer_phone: str | None = None,
        channel: str | None = None,
        channel_user_id: str | None = None,
        booking_id: int | None = None,
    ) -> int:
        """Must run inside a write transaction."""
        handoff_id = self.db.execute(
            """
            INSERT INTO handoffs (created_at, type, summary, customer_name, customer_phone,
                                  channel, channel_user_id, booking_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (self._timestamp(), handoff_type.value, summary, customer_name, customer_phone,
             channel, channel_user_id, booking_id),
        ).lastrowid
        if booking_id:
            self._record_event(booking_id, actor, "handed_off", details=f"{handoff_type.value}: handoff #{handoff_id}")
        return handoff_id

    # --- Owner actions ----------------------------------------------------------------------

    def approve_payment(self, reference: str, note: str = "") -> Approval:
        """Owner: the payment arrived in the account. Confirm the booking and block its date.

        Allowed from PENDING_PAYMENT too - the screenshot is optional. Every other
        pending booking for the same date is flagged as a date conflict and handed
        to the owner. Nothing is refunded, cancelled or sent to those customers.
        """
        with self._write_transaction():
            booking = self._require_booking(reference)
            if booking.booking_date < self.today():
                raise BookingRefused(RefusalReason.PAST_DATE, f"{booking.reference} is for a date that has passed")
            if BookingStatus.CONFIRMED not in ALLOWED_STATUS_CHANGES[booking.status]:
                raise BookingRefused(RefusalReason.WRONG_STATUS, f"{booking.reference} is {booking.status}")
            if self._date_is_blocked(booking.booking_date):
                raise BookingRefused(
                    RefusalReason.DATE_TAKEN, f"{booking.booking_date} already has a confirmed booking"
                )
            self._change_status(booking, BookingStatus.CONFIRMED, Actor.OWNER, note)
            self._set_date_conflict(booking.id, False)  # the owner has decided for this one
            conflicts = self._flag_conflicts(booking)
            self._queue_confirmation_and_reminder(booking.id)
        return Approval(
            booking=self._get_booking_by_id(booking.id),
            conflicts=[self._get_booking_by_id(conflict.id) for conflict in conflicts],
        )

    def reject_payment(self, reference: str, note: str = "") -> Booking:
        """Owner: the screenshot doesn't match a real payment. Back to PENDING_PAYMENT."""
        with self._write_transaction():
            booking = self._require_booking(reference)
            if booking.status is not BookingStatus.PAYMENT_SUBMITTED:
                raise BookingRefused(
                    RefusalReason.WRONG_STATUS, f"{booking.reference} is {booking.status} - no screenshot to reject"
                )
            self._change_status(booking, BookingStatus.PENDING_PAYMENT, Actor.OWNER, note)
        return self._get_booking_by_id(booking.id)

    def cancel_booking(self, reference: str, note: str = "") -> Booking:
        """Owner: cancel. Frees the date. Any refund is handled by the owner outside the system."""
        with self._write_transaction():
            booking = self._require_booking(reference)
            self._change_status(booking, BookingStatus.CANCELLED, Actor.OWNER, note)
            self._cancel_waiting_messages(booking.id, "booking cancelled")
        return self._get_booking_by_id(booking.id)

    def complete_booking(self, reference: str, note: str = "") -> Booking:
        """Owner: the setup took place."""
        with self._write_transaction():
            booking = self._require_booking(reference)
            if booking.booking_date > self.today():
                raise BookingRefused(RefusalReason.TOO_EARLY, f"{booking.reference} is not until {booking.booking_date}")
            self._change_status(booking, BookingStatus.COMPLETED, Actor.OWNER, note)
            self._cancel_waiting_messages(booking.id, "booking completed")
        return self._get_booking_by_id(booking.id)

    def reschedule_booking(self, reference: str, new_date: date, note: str = "") -> Approval:
        """Owner: move a booking to another date, if that date is available.

        Status, payment choice and amounts stay as they are - any payment
        adjustment is handled by the owner outside the system. Today is allowed:
        same-day decisions belong to the owner. If a confirmed booking moves,
        pending bookings on its new date are flagged, exactly as on approval.
        """
        with self._write_transaction():
            booking = self._require_booking(reference)
            if booking.status in (BookingStatus.COMPLETED, BookingStatus.CANCELLED):
                raise BookingRefused(RefusalReason.WRONG_STATUS, f"{booking.reference} is {booking.status}")
            if new_date < self.today():
                raise BookingRefused(RefusalReason.PAST_DATE, f"{new_date} has passed")
            if new_date == booking.booking_date:
                raise BookingRefused(RefusalReason.WRONG_STATUS, f"{booking.reference} is already on {new_date}")
            if self._date_is_blocked(new_date):
                raise BookingRefused(RefusalReason.DATE_TAKEN, f"{new_date} already has a confirmed booking")

            self.db.execute(
                "UPDATE bookings SET booking_date = ?, date_conflict = 0, updated_at = ? WHERE id = ?",
                (new_date.isoformat(), self._timestamp(), booking.id),
            )
            details = f"{booking.booking_date} -> {new_date}" + (f"; {note}" if note else "")
            self._record_event(booking.id, Actor.OWNER, "rescheduled", details=details)
            booking = self._get_booking_by_id(booking.id)
            conflicts = []
            if booking.status is BookingStatus.CONFIRMED:
                conflicts = self._flag_conflicts(booking)
                # Waiting messages mention the old date: cancel them. The reminder is
                # queued again for the new date; telling the customer about the move
                # stays with the owner (no automatic reschedule message).
                self._cancel_waiting_messages(booking.id, "booking rescheduled")
                self._record_event(booking.id, Actor.SYSTEM, "outbox",
                                   details=outbox.queue_reminder(self.db, self.info, booking, self.now()))
        return Approval(
            booking=self._get_booking_by_id(booking.id),
            conflicts=[self._get_booking_by_id(conflict.id) for conflict in conflicts],
        )

    def _queue_confirmation_and_reminder(self, booking_id: int) -> None:
        """Must run inside a write transaction - the messages exist exactly when the confirmation does."""
        booking = self._get_booking_by_id(booking_id)
        for done in outbox.queue_confirmation_and_reminder(self.db, self.info, booking, self.now()):
            self._record_event(booking.id, Actor.SYSTEM, "outbox", details=done)

    def _cancel_waiting_messages(self, booking_id: int, reason: str) -> None:
        """Must run inside a write transaction."""
        cancelled = outbox.cancel_waiting(self.db, booking_id, self.now())
        if cancelled:
            self._record_event(booking_id, Actor.SYSTEM, "outbox",
                               details=f"{cancelled} waiting message(s) cancelled: {reason}")

    def _flag_conflicts(self, confirmed: Booking) -> list[Booking]:
        """Flag every other waiting booking on the confirmed booking's date, and hand
        each one to the owner. Must run inside a write transaction."""
        rows = self.db.execute(
            "SELECT * FROM bookings WHERE booking_date = ? AND id != ? AND status IN (?, ?) ORDER BY id",
            (confirmed.booking_date.isoformat(), confirmed.id,
             BookingStatus.PENDING_PAYMENT.value, BookingStatus.PAYMENT_SUBMITTED.value),
        )
        conflicts = [Booking.from_row(row) for row in rows]
        for conflict in conflicts:
            self._set_date_conflict(conflict.id, True)
            self._record_event(
                conflict.id, Actor.SYSTEM, "date_conflict_flagged",
                details=f"{confirmed.reference} was confirmed for {confirmed.booking_date}",
            )
            screenshot = "sent a payment screenshot" if conflict.payment_proof else "no payment screenshot"
            self._insert_handoff(
                HandoffType.DATE_CONFLICT,
                f"{confirmed.reference} was confirmed for {confirmed.booking_date}. "
                f"{conflict.reference} ({conflict.status}, {screenshot}) is for the same date "
                "and needs your decision. The customer has not been told anything.",
                Actor.SYSTEM,
                customer_name=conflict.customer_name,
                customer_phone=conflict.customer_phone,
                channel=conflict.channel,
                channel_user_id=conflict.channel_user_id,
                booking_id=conflict.id,
            )
        return conflicts

    def _set_date_conflict(self, booking_id: int, flagged: bool) -> None:
        self.db.execute("UPDATE bookings SET date_conflict = ? WHERE id = ?", (int(flagged), booking_id))

    def _require_booking(self, reference: str) -> Booking:
        booking = self.get_booking(reference)
        if booking is None:
            raise BookingRefused(RefusalReason.BOOKING_NOT_FOUND, f"no booking {reference!r}")
        return booking

    # --- Reading ------------------------------------------------------------------------

    def get_booking(self, reference: str) -> Booking | None:
        row = self.db.execute(
            "SELECT * FROM bookings WHERE reference = ?", (reference.strip().upper(),)
        ).fetchone()
        return Booking.from_row(row) if row else None

    def list_bookings(
        self, *, statuses: tuple[BookingStatus, ...] | None = None, on_date: date | None = None
    ) -> list[Booking]:
        """Bookings in date order, optionally only some statuses and/or one date."""
        conditions, values = [], []
        if statuses:
            conditions.append(f"status IN ({', '.join('?' for _ in statuses)})")
            values.extend(status.value for status in statuses)
        if on_date:
            conditions.append("booking_date = ?")
            values.append(on_date.isoformat())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.db.execute(f"SELECT * FROM bookings {where} ORDER BY booking_date, id", values)
        return [Booking.from_row(row) for row in rows]

    def booking_history(self, reference: str) -> list[sqlite3.Row]:
        """Everything that happened to a booking, oldest first."""
        return self.db.execute(
            """
            SELECT e.* FROM booking_events e JOIN bookings b ON b.id = e.booking_id
            WHERE b.reference = ? ORDER BY e.id
            """,
            (reference.strip().upper(),),
        ).fetchall()

    def get_booking_by_id(self, booking_id: int) -> Booking | None:
        row = self.db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return Booking.from_row(row) if row else None

    def _get_booking_by_id(self, booking_id: int) -> Booking:
        return Booking.from_row(self.db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone())

    def _date_is_blocked(self, booking_date: date) -> bool:
        placeholders = ", ".join("?" for _ in BLOCKING_STATUSES)
        row = self.db.execute(
            f"SELECT 1 FROM bookings WHERE booking_date = ? AND status IN ({placeholders})",
            (booking_date.isoformat(), *BLOCKING_STATUSES),
        ).fetchone()
        return row is not None

    # --- Helpers --------------------------------------------------------------------------

    def _timestamp(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def _change_status(self, booking: Booking, new_status: BookingStatus, actor: Actor, details: str = "") -> None:
        """The only place a booking's status changes. Must run inside a write transaction."""
        if new_status not in ALLOWED_STATUS_CHANGES[booking.status]:
            raise BookingRefused(
                RefusalReason.WRONG_STATUS, f"{booking.reference} cannot go from {booking.status} to {new_status}"
            )
        self.db.execute(
            "UPDATE bookings SET status = ?, updated_at = ? WHERE id = ?",
            (new_status.value, self._timestamp(), booking.id),
        )
        self._record_event(
            booking.id, actor, "status_changed", old_status=booking.status, new_status=new_status, details=details
        )

    def _record_event(
        self,
        booking_id: int,
        actor: Actor,
        event: str,
        *,
        old_status: BookingStatus | None = None,
        new_status: BookingStatus | None = None,
        details: str = "",
    ) -> None:
        self.db.execute(
            """
            INSERT INTO booking_events (booking_id, happened_at, actor, event, old_status, new_status, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (booking_id, self._timestamp(), actor.value, event, old_status, new_status, details),
        )

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        """A group of changes that happens completely or not at all.

        IMMEDIATE takes the write lock at the start, so no other part of the
        system (the owner's commands, another conversation) can change
        bookings between our checks and our writes.
        """
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        self.db.commit()
