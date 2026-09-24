"""The owner's commands: see and manage bookings and handoffs.

    uv run cozysetup-admin --help
    uv run cozysetup-admin pending
    uv run cozysetup-admin show CS-0001
    uv run cozysetup-admin approve CS-0001 "25 KWD received"
    uv run cozysetup-admin overview

Every command goes through the BookingService, so every booking rule applies.
Add --db PATH to practise on a separate database instead of the real one.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from cozysetup.bookings import Approval, Booking, BookingRefused, BookingService, DateStatus, Handoff
from cozysetup.business_info import (
    BusinessInfo,
    BusinessInfoError,
    format_time_en,
    load_business_info,
    normalize_name,
)
from cozysetup.database import DEFAULT_DB_PATH, BookingStatus, HandoffStatus, HandoffType, connect
from cozysetup.rules import PaymentChoice, format_kwd

STATUS_LABELS = {
    BookingStatus.PENDING_PAYMENT: "waiting for payment",
    BookingStatus.PAYMENT_SUBMITTED: "screenshot received - check your account",
    BookingStatus.CONFIRMED: "confirmed",
    BookingStatus.COMPLETED: "completed",
    BookingStatus.CANCELLED: "cancelled",
}
WAITING = (BookingStatus.PENDING_PAYMENT, BookingStatus.PAYMENT_SUBMITTED)

HANDOFF_LABELS = {
    HandoffType.SAME_DAY: "same-day request",
    HandoffType.CANCELLATION: "cancellation",
    HandoffType.RESCHEDULE: "reschedule",
    HandoffType.WEATHER: "weather",
    HandoffType.DAMAGE: "damage",
    HandoffType.PAYMENT: "payment",
    HandoffType.DATE_CONFLICT: "date conflict",
    HandoffType.OTHER: "other",
}


# --- Showing things the way a person reads them ---------------------------------

class Display:
    def __init__(self, info: BusinessInfo, today: date):
        self.info = info
        self.today = today
        self._location_names = {location.id: location.name.en for location in info.locations}

    def money(self, fils: int) -> str:
        return f"{format_kwd(fils)} {self.info.pricing.currency}"

    def day(self, value: date) -> str:
        """date(2026, 10, 1) -> "Thu 1 Oct 2026", plus "today"/"tomorrow" when it is."""
        text = f"{value:%a} {value.day} {value:%b %Y}"
        difference = (value - self.today).days
        if difference == 0:
            return f"{text} (today)"
        if difference == 1:
            return f"{text} (tomorrow)"
        return text

    def location(self, location_id: str) -> str:
        return self._location_names.get(location_id, location_id)

    def status(self, booking: Booking) -> str:
        label = STATUS_LABELS[booking.status]
        if booking.date_conflict:
            label += "  ⚠ DATE CONFLICT - see handoffs"
        return label

    def payment(self, booking: Booking) -> str:
        paid_now = self.money(booking.amounts.amount_now)
        if booking.payment_choice is PaymentChoice.DEPOSIT:
            return f"deposit {paid_now}"
        return f"full {paid_now}"

    def due_on_day(self, booking: Booking) -> str:
        parts = []
        if booking.amounts.remaining_on_day:
            parts.append(f"{self.money(booking.amounts.remaining_on_day)} remaining")
        parts.append(f"{self.money(booking.amounts.security_deposit)} security deposit")
        return " + ".join(parts)

    def table(self, bookings: list[Booking]) -> str:
        rows = [("REF", "DATE", "LOCATION", "CUSTOMER", "PAYS VIA WAMD", "STATUS")]
        for booking in bookings:
            rows.append((
                booking.reference,
                self.day(booking.booking_date),
                self.location(booking.location_id),
                f"{booking.customer_name} {booking.customer_phone}",
                self.payment(booking),
                self.status(booking),
            ))
        widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]) - 1)]
        return "\n".join(
            "  ".join(cell.ljust(width) for cell, width in zip(row, widths)) + "  " + row[-1]
            for row in rows
        )


# --- What every command gets -----------------------------------------------------------

def open_with_system_viewer(path: Path) -> None:
    """Open a file the way double-clicking it would (Preview on a Mac)."""
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        print(f"  Open it yourself: {path}")


@dataclass
class Context:
    service: BookingService
    show: Display
    ask: Callable[[str], str]           # asks the owner a question; normally input()
    open_file: Callable[[Path], None]   # shows a file; normally Preview

    def date(self, value: date | str) -> date:
        """Turn "today"/"tomorrow" into a date (Kuwait time)."""
        if value == "today":
            return self.show.today
        if value == "tomorrow":
            return self.show.today + timedelta(days=1)
        return value

    def confirm(self, args: argparse.Namespace, lines: list[str]) -> bool:
        """Show what is about to happen and ask. Enter (or no answer) means No."""
        print("\n".join(lines))
        if args.yes:
            return True
        try:
            answer = self.ask("Confirm? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() in ("y", "yes"):
            return True
        print("Nothing changed.")
        return False


# --- Looking (changes nothing) ------------------------------------------------------------
# Each command prints its result and returns the exit code (0 = success).

def command_bookings(ctx: Context, args: argparse.Namespace) -> int:
    service, show = ctx.service, ctx.show
    statuses = tuple(BookingStatus(status) for status in args.status) if args.status else None
    on_date = ctx.date(args.date) if args.date else None
    bookings = service.list_bookings(statuses=statuses, on_date=on_date)
    if not args.all and args.date is None:
        bookings = [booking for booking in bookings if booking.booking_date >= show.today]
    if not bookings:
        print("No bookings found." if args.all or args.date else "No upcoming bookings.")
        return 0
    print(show.table(bookings))
    return 0


def command_pending(ctx: Context, args: argparse.Namespace) -> int:
    service, show = ctx.service, ctx.show
    bookings = service.list_bookings(statuses=WAITING)
    if not bookings:
        print("Nothing is waiting for payment or approval.")
        return 0
    print(f"{len(bookings)} booking(s) waiting for payment or your approval:\n")
    print(show.table(bookings))
    print("\nApprove one after checking the money arrived:  uv run cozysetup-admin approve CS-XXXX \"note\"")
    return 0


def command_show(ctx: Context, args: argparse.Namespace) -> int:
    service, show = ctx.service, ctx.show
    booking = service.get_booking(args.reference)
    if booking is None:
        print(f"✗ No booking {args.reference.strip().upper()}", file=sys.stderr)
        return 1
    print(f"{booking.reference}  -  {show.status(booking)}")
    print()
    pricing = show.info.pricing
    print(f"  Date:        {show.day(booking.booking_date)}, "
          f"{format_time_en(pricing.start_time)} – {format_time_en(pricing.end_time)}")
    print(f"  Location:    {show.location(booking.location_id)}")
    print(f"  Customer:    {booking.customer_name}  {booking.customer_phone}")
    print(f"  Came via:    {booking.channel}")
    print(f"  Rental:      {show.money(booking.amounts.rental_price)}")
    print(f"  Via Wamd:    {show.payment(booking)}")
    print(f"  On the day:  {show.due_on_day(booking)}")
    print(f"  Screenshot:  {booking.payment_proof or 'none'}")
    print()
    print("  History:")
    for event in service.booking_history(booking.reference):
        happened = datetime.fromisoformat(event["happened_at"])
        change = f"{event['old_status'] or '-'} -> {event['new_status']}" if event["new_status"] else ""
        line = f"    {happened:%d %b %H:%M}  {event['actor']:<6}  {event['event']:<22} {change}"
        if event["details"]:
            line += f"  ({event['details']})"
        print(line.rstrip())
    return 0


# --- Acting (changes something - always asks first) ------------------------------------

NOT_TOLD = "The customer has not been messaged - let them know yourself for now."


def command_add(ctx: Context, args: argparse.Namespace) -> int:
    service, show = ctx.service, ctx.show
    booking_date = ctx.date(args.date)
    location_id = resolve_location(show.info, args.location)
    if location_id is None:
        names = ", ".join(location.name.en for location in show.info.locations)
        return fail(f"Unknown location {args.location!r}. Locations: {names}")

    # Check before asking, so the owner isn't asked to confirm something that will fail.
    # The service checks everything again when it saves.
    availability = service.check_availability(booking_date, for_owner=True)
    if availability.status is DateStatus.PAST:
        return fail(f"{show.day(booking_date)} has passed")
    if availability.status is DateStatus.TAKEN:
        return fail(f"{show.day(booking_date)} already has a confirmed booking")
    allowed = [choice.value for choice in availability.options]
    if args.payment not in allowed:
        return fail(f"For {show.day(booking_date)} the payment must be: {' or '.join(allowed)}")

    amounts = availability.options[PaymentChoice(args.payment)]
    lines = [
        "Add booking?",
        f"  {args.name} ({args.phone}) - {show.day(booking_date)}, {show.location(location_id)}",
        f"  Via Wamd: {args.payment} {show.money(amounts.amount_now)}",
    ]
    if args.paid:
        lines.append(f"  --paid: you confirm you have received {show.money(amounts.amount_now)}. "
                     "It will be CONFIRMED now and block the date.")
        lines.extend(conflict_warning(ctx, booking_date))
    else:
        lines.append("  It will wait for payment (approve it once paid).")
    if not ctx.confirm(args, lines):
        return 1

    result = service.create_owner_booking(
        booking_date=booking_date, location_id=location_id, customer_name=args.name,
        customer_phone=args.phone, payment_choice=args.payment, paid=args.paid,
    )
    print(f"✓ {result.booking.reference} created - {show.status(result.booking)}")
    report_conflicts(show, result)
    return 0


def command_approve(ctx: Context, args: argparse.Namespace) -> int:
    booking = ctx.service.get_booking(args.reference)
    if booking is None:
        return fail(f"No booking {args.reference.strip().upper()}")
    show = ctx.show
    if booking.status not in WAITING:
        return fail(f"{booking.reference} is {STATUS_LABELS[booking.status]} - only waiting bookings can be approved")
    if booking.booking_date < show.today:
        return fail(f"{booking.reference} was for {show.day(booking.booking_date)}, which has passed")
    if ctx.service.check_availability(booking.booking_date, for_owner=True).status is DateStatus.TAKEN:
        return fail(f"{show.day(booking.booking_date)} already has a confirmed booking")
    lines = [
        f"Approve {booking.reference}? You confirm {show.money(booking.amounts.amount_now)} arrived in your account.",
        f"  {describe(show, booking)}",
    ]
    if not booking.payment_proof:
        lines.append("  No screenshot was sent - make sure you checked the payment in your account.")
    lines.extend(conflict_warning(ctx, booking.booking_date, except_booking=booking))
    if not ctx.confirm(args, lines):
        return 1
    result = ctx.service.approve_payment(booking.reference, args.note)
    print(f"✓ {booking.reference} confirmed - {show.day(booking.booking_date)} is now blocked.")
    report_conflicts(show, result)
    print(NOT_TOLD)
    return 0


def command_reject(ctx: Context, args: argparse.Namespace) -> int:
    booking = ctx.service.get_booking(args.reference)
    if booking is None:
        return fail(f"No booking {args.reference.strip().upper()}")
    if booking.status is not BookingStatus.PAYMENT_SUBMITTED:
        return fail(f"{booking.reference} is {STATUS_LABELS[booking.status]} - there is no screenshot to reject")
    lines = [
        f"Reject the payment screenshot for {booking.reference}? It goes back to waiting for payment.",
        f"  {describe(ctx.show, booking)}",
    ]
    if not ctx.confirm(args, lines):
        return 1
    ctx.service.reject_payment(booking.reference, args.note)
    print(f"✓ {booking.reference} is waiting for payment again. The customer can send a new screenshot.")
    print(NOT_TOLD)
    return 0


def command_cancel(ctx: Context, args: argparse.Namespace) -> int:
    service, show = ctx.service, ctx.show
    booking = service.get_booking(args.reference)
    if booking is None:
        return fail(f"No booking {args.reference.strip().upper()}")
    if booking.status in (BookingStatus.COMPLETED, BookingStatus.CANCELLED):
        return fail(f"{booking.reference} is already {booking.status}")
    lines = [f"Cancel {booking.reference}?", f"  {describe(show, booking)}"]
    if booking.status is BookingStatus.CONFIRMED:
        lines.append(f"  They paid {show.money(booking.amounts.amount_now)}. "
                     "Any refund is up to you, outside the system.")
    if not ctx.confirm(args, lines):
        return 1
    service.cancel_booking(booking.reference, args.note)
    print(f"✓ {booking.reference} cancelled.")
    if booking.status is BookingStatus.CONFIRMED:
        print(f"  {show.day(booking.booking_date)} is free again.")
        waiting = [b for b in service.list_bookings(statuses=WAITING, on_date=booking.booking_date)]
        if waiting:
            print("  Still waiting for that date (you may approve one): "
                  + ", ".join(b.reference for b in waiting))
    print(NOT_TOLD)
    return 0


def command_reschedule(ctx: Context, args: argparse.Namespace) -> int:
    service, show = ctx.service, ctx.show
    booking = service.get_booking(args.reference)
    if booking is None:
        return fail(f"No booking {args.reference.strip().upper()}")
    if booking.status in (BookingStatus.COMPLETED, BookingStatus.CANCELLED):
        return fail(f"{booking.reference} is {booking.status} - it can't be moved")
    new_date = ctx.date(args.new_date)
    if new_date == booking.booking_date:
        return fail(f"{booking.reference} is already on {show.day(new_date)}")
    availability = service.check_availability(new_date, for_owner=True)
    if availability.status is DateStatus.PAST:
        return fail(f"{show.day(new_date)} has passed")
    if availability.status is DateStatus.TAKEN:
        return fail(f"{show.day(new_date)} already has a confirmed booking")

    lines = [
        f"Move {booking.reference} from {show.day(booking.booking_date)} to {show.day(new_date)}?",
        f"  {describe(show, booking)}",
        "  Status and amounts stay the same.",
    ]
    if booking.status is BookingStatus.CONFIRMED:
        lines.extend(conflict_warning(ctx, new_date))
    if not ctx.confirm(args, lines):
        return 1
    result = service.reschedule_booking(booking.reference, new_date, args.note)
    print(f"✓ {booking.reference} moved to {show.day(new_date)}.")
    report_conflicts(show, result)
    print(NOT_TOLD)
    return 0


def command_complete(ctx: Context, args: argparse.Namespace) -> int:
    booking = ctx.service.get_booking(args.reference)
    if booking is None:
        return fail(f"No booking {args.reference.strip().upper()}")
    if booking.status is not BookingStatus.CONFIRMED:
        return fail(f"{booking.reference} is {STATUS_LABELS[booking.status]} - only confirmed bookings can be completed")
    if booking.booking_date > ctx.show.today:
        return fail(f"{booking.reference} is not until {ctx.show.day(booking.booking_date)}")
    if not ctx.confirm(args, [f"Mark {booking.reference} as completed?", f"  {describe(ctx.show, booking)}"]):
        return 1
    ctx.service.complete_booking(booking.reference, args.note)
    print(f"✓ {booking.reference} completed.")
    return 0


# --- Handoffs, screenshots and the overview -----------------------------------------------

def command_handoffs(ctx: Context, args: argparse.Namespace) -> int:
    handoffs = ctx.service.list_handoffs(status=None if args.all else HandoffStatus.OPEN)
    if not handoffs:
        print("No handoffs." if args.all else "No open handoffs - nothing handed to you.")
        return 0
    for handoff in handoffs:
        print(describe_handoff(ctx, handoff))
        print()
    if not args.all:
        print('When dealt with:  uv run cozysetup-admin resolve <number> "what you did"')
    return 0


def command_resolve(ctx: Context, args: argparse.Namespace) -> int:
    handoff = ctx.service.get_handoff(args.number)
    if handoff is None:
        return fail(f"No handoff #{args.number}")
    if handoff.status is HandoffStatus.RESOLVED:
        return fail(f"Handoff #{handoff.id} is already resolved")
    if not ctx.confirm(args, ["Mark this handoff as resolved?", describe_handoff(ctx, handoff)]):
        return 1
    ctx.service.resolve_handoff(handoff.id, args.note)
    print(f"✓ Handoff #{handoff.id} resolved.")
    return 0


def command_proof(ctx: Context, args: argparse.Namespace) -> int:
    booking = ctx.service.get_booking(args.reference)
    if booking is None:
        return fail(f"No booking {args.reference.strip().upper()}")
    if not booking.payment_proof:
        print(f"{booking.reference} has no payment screenshot.")
        return 0
    path = ctx.service.proofs_dir / booking.payment_proof
    if not path.exists():
        return fail(f"The screenshot file is missing: {path}")
    earlier = sorted(p.name for p in ctx.service.proofs_dir.glob(f"{booking.reference}-*") if p != path)
    print(f"Opening {path.name} - remember: a screenshot is not proof, check your account.")
    if earlier:
        print(f"  Earlier screenshots for this booking: {', '.join(earlier)}")
    ctx.open_file(path)
    return 0


def command_overview(ctx: Context, args: argparse.Namespace) -> int:
    service, show = ctx.service, ctx.show
    today = show.today
    confirmed = service.list_bookings(statuses=(BookingStatus.CONFIRMED,))
    submitted = service.list_bookings(statuses=(BookingStatus.PAYMENT_SUBMITTED,))
    pending = service.list_bookings(statuses=(BookingStatus.PENDING_PAYMENT,))
    handoffs = service.list_handoffs()

    print(f"CozySetup overview - {show.day(today)}")

    print("\nNEEDS YOUR ATTENTION")
    attention = False
    if handoffs:
        attention = True
        print(f"  {len(handoffs)} open handoff(s) - see: handoffs")
        for handoff in handoffs[:5]:
            print(f"    #{handoff.id} {HANDOFF_LABELS[handoff.type]}: {first_line(handoff.summary)}")
    if submitted:
        attention = True
        print(f"  {len(submitted)} screenshot(s) to check in your account, then approve or reject:")
        for booking in submitted:
            conflict = "  ⚠ DATE CONFLICT" if booking.date_conflict else ""
            print(f"    {booking.reference}  {show.day(booking.booking_date)}  {booking.customer_name}  "
                  f"{show.payment(booking)}{conflict}")
    to_complete = [booking for booking in confirmed if booking.booking_date < today]
    if to_complete:
        attention = True
        print("  Past setups to mark as completed: " + ", ".join(b.reference for b in to_complete))
    if not attention:
        print("  Nothing - all clear.")

    print("\nTODAY")
    todays = [booking for booking in confirmed if booking.booking_date == today]
    if not todays:
        print("  No setup today.")
    for booking in todays:
        print(f"  {booking.reference}  {show.location(booking.location_id)}  "
              f"{booking.customer_name} {booking.customer_phone}")
        print(f"    Collect on arrival: {show.due_on_day(booking)}")

    print("\nNEXT 7 DAYS (confirmed)")
    upcoming = [booking for booking in confirmed if today < booking.booking_date <= today + timedelta(days=7)]
    if not upcoming:
        print("  None.")
    for booking in upcoming:
        print(f"  {show.day(booking.booking_date):<28} {show.location(booking.location_id):<22} "
              f"{booking.customer_name}  ({booking.reference})")

    waiting = [booking for booking in pending if booking.booking_date >= today]
    print(f"\nWAITING FOR PAYMENT: {len(waiting)} booking(s) - see: pending")
    return 0


def describe_handoff(ctx: Context, handoff: Handoff) -> str:
    title = f"#{handoff.id}  [{HANDOFF_LABELS[handoff.type]}]  {handoff.created_at:%a %d %b %H:%M}"
    lines = [title]
    customer = " ".join(part for part in (handoff.customer_name, handoff.customer_phone) if part)
    if handoff.booking_id:
        booking = ctx.service.get_booking_by_id(handoff.booking_id)
        customer = f"{booking.reference} - {customer}" if customer else booking.reference
    if customer:
        lines.append(f"    Customer: {customer}")
    lines.extend(f"    {line}" for line in handoff.summary.splitlines())
    if handoff.status is HandoffStatus.RESOLVED:
        lines.append(f"    ✓ Resolved {handoff.resolved_at:%a %d %b %H:%M}: {handoff.resolution_note or '(no note)'}")
    return "\n".join(lines)


def first_line(text: str, limit: int = 70) -> str:
    line = text.splitlines()[0] if text else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


# --- Helpers for the acting commands ------------------------------------------------------

def fail(message: str) -> int:
    print(f"✗ {message}", file=sys.stderr)
    return 1


def describe(show: Display, booking: Booking) -> str:
    return (f"{booking.customer_name} ({booking.customer_phone}) - {show.day(booking.booking_date)}, "
            f"{show.location(booking.location_id)} - {show.status(booking)}")


def conflict_warning(ctx: Context, booking_date: date, except_booking: Booking | None = None) -> list[str]:
    waiting = [
        booking for booking in ctx.service.list_bookings(statuses=WAITING, on_date=booking_date)
        if except_booking is None or booking.id != except_booking.id
    ]
    if not waiting:
        return []
    references = ", ".join(booking.reference for booking in waiting)
    return [f"  ⚠ {len(waiting)} other booking(s) waiting for this date will be flagged "
            f"and added to your handoffs: {references}"]


def report_conflicts(show: Display, result: Approval) -> None:
    for conflict in result.conflicts:
        print(f"  ⚠ {conflict.reference} ({conflict.customer_name}) was waiting for the same date - "
              "flagged and added to your handoffs.")


def resolve_location(info: BusinessInfo, text: str) -> str | None:
    """A location id from its id, English or Arabic name, or any alias."""
    wanted = normalize_name(text)
    for location in info.locations:
        names = (location.id, location.name.en, location.name.ar, *location.aliases)
        if wanted in {normalize_name(name) for name in names}:
            return location.id
    return None


# --- Reading what the owner typed -------------------------------------------------------

def parse_date(text: str) -> date | str:
    """A date like 2026-10-01, or the words today / tomorrow (turned into dates later)."""
    if text.lower() in ("today", "tomorrow"):
        return text.lower()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not a date - write it like 2026-10-01, or today / tomorrow"
        ) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cozysetup-admin", description="CozySetup.kw - the owner's commands.",
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH,
                        help="database file to use (default: the real one). Use another to practise.")
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")

    overview = commands.add_parser("overview", help="everything that needs your attention, on one screen")
    overview.set_defaults(handler=command_overview)

    bookings = commands.add_parser("bookings", help="list upcoming bookings")
    bookings.add_argument("--status", action="append", choices=[s.value for s in BookingStatus],
                          help="only this status (can be repeated)")
    bookings.add_argument("--date", type=parse_date, help="only this date, e.g. 2026-10-01")
    bookings.add_argument("--all", action="store_true", help="include past bookings")
    bookings.set_defaults(handler=command_bookings)

    pending = commands.add_parser("pending", help="bookings waiting for payment or your approval")
    pending.set_defaults(handler=command_pending)

    show = commands.add_parser("show", help="everything about one booking, including its history")
    show.add_argument("reference", help="e.g. CS-0001")
    show.set_defaults(handler=command_show)

    proof = commands.add_parser("proof", help="open a booking's payment screenshot")
    proof.add_argument("reference", help="e.g. CS-0001")
    proof.set_defaults(handler=command_proof)

    handoffs = commands.add_parser("handoffs", help="your to-do list: everything the AI handed to you")
    handoffs.add_argument("--all", action="store_true", help="include resolved handoffs")
    handoffs.set_defaults(handler=command_handoffs)

    def acting(name: str, help_text: str, handler) -> argparse.ArgumentParser:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
        command.set_defaults(handler=handler)
        return command

    add = acting("add", "add a booking yourself (e.g. by phone, or same-day)", command_add)
    add.add_argument("--date", type=parse_date, required=True, help="e.g. 2026-10-01, today, tomorrow")
    add.add_argument("--location", required=True, help="e.g. julaia, Julaia, Arifjan")
    add.add_argument("--name", required=True)
    add.add_argument("--phone", required=True, help="e.g. 99999999 or +966501234567")
    add.add_argument("--payment", required=True, choices=[c.value for c in PaymentChoice])
    add.add_argument("--paid", action="store_true",
                     help="you have verified/received the payment: confirm it now")

    for name, help_text, handler in (
        ("approve", "payment verified in your account: confirm the booking", command_approve),
        ("reject", "screenshot doesn't match a payment: back to waiting for payment", command_reject),
        ("cancel", "cancel a booking (refunds are up to you, outside the system)", command_cancel),
        ("complete", "the setup took place", command_complete),
    ):
        command = acting(name, help_text, handler)
        command.add_argument("reference", help="e.g. CS-0001")
        command.add_argument("note", nargs="?", default="", help="optional note, kept in the history")

    resolve = acting("resolve", "mark a handoff as dealt with", command_resolve)
    resolve.add_argument("number", type=int, help="the handoff number, e.g. 3")
    resolve.add_argument("note", nargs="?", default="", help="what you did, kept with the handoff")

    reschedule = acting("reschedule", "move a booking to another available date", command_reschedule)
    reschedule.add_argument("reference", help="e.g. CS-0001")
    reschedule.add_argument("new_date", type=parse_date, help="e.g. 2026-10-08, today, tomorrow")
    reschedule.add_argument("note", nargs="?", default="", help="optional note, kept in the history")

    return parser


def main(
    argv: list[str] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    ask: Callable[[str], str] = input,
    open_file: Callable[[Path], None] = open_with_system_viewer,
) -> int:
    """`clock` and `ask` let tests pretend the time and the owner's answers;
    normally the real time and the keyboard are used."""
    args = build_parser().parse_args(argv)
    try:
        info = load_business_info()
    except BusinessInfoError as error:
        print(error, file=sys.stderr)
        return 1

    db = connect(args.db)
    try:
        # Screenshots live next to the database, so a practice database has its own.
        service = BookingService(db, info, clock=clock, proofs_dir=args.db.parent / "payment_proofs")
        return args.handler(Context(service, Display(info, service.today()), ask, open_file), args)
    except BookingRefused as refused:
        print(f"✗ {refused}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
