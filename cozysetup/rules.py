"""Booking rules that need no database: dates, payment options and amounts.

Every function takes "today" as an input instead of reading the clock, so
tests can pretend it is any day. Only today_in() reads the real clock.

All money is in fils (1 KWD = 1000 fils) - whole numbers are always exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from zoneinfo import ZoneInfo

from cozysetup.business_info import Payment, Pricing

FILS_PER_KWD = 1000


class DateWindow(StrEnum):
    """Where a booking date falls, before looking at other bookings."""
    PAST = "past"
    SAME_DAY = "same_day"   # handed to the owner - never booked by the AI
    BOOKABLE = "bookable"   # tomorrow or later


class PaymentChoice(StrEnum):
    DEPOSIT = "deposit"
    FULL = "full"


@dataclass(frozen=True)
class Amounts:
    """What a booking costs, in fils. Fixed when the booking is created."""
    rental_price: int
    amount_now: int         # paid via Wamd to secure the booking
    remaining_on_day: int   # rest of the rental price, paid on arrival
    security_deposit: int   # paid on arrival, refundable


def today_in(timezone: ZoneInfo) -> date:
    """Today's date in the business's timezone. The only function that reads the clock."""
    return datetime.now(timezone).date()


def days_ahead(booking_date: date, today: date) -> int:
    """Calendar days from today to the booking date: tomorrow = 1, today = 0."""
    return (booking_date - today).days


def date_window(booking_date: date, today: date) -> DateWindow:
    ahead = days_ahead(booking_date, today)
    if ahead < 0:
        return DateWindow.PAST
    if ahead == 0:
        return DateWindow.SAME_DAY
    return DateWindow.BOOKABLE


def payment_options(booking_date: date, today: date, payment: Payment) -> tuple[PaymentChoice, ...]:
    """The payment choices the customer may pick for this date.

    Booked far enough ahead: 50% deposit or full payment. Otherwise: full only.
    """
    if date_window(booking_date, today) is not DateWindow.BOOKABLE:
        raise ValueError(f"{booking_date} is not bookable when today is {today}")
    if days_ahead(booking_date, today) >= payment.deposit_min_days_ahead:
        return (PaymentChoice.DEPOSIT, PaymentChoice.FULL)
    return (PaymentChoice.FULL,)


def calculate_amounts(choice: PaymentChoice, pricing: Pricing, payment: Payment) -> Amounts:
    rental_price = kwd_to_fils(pricing.base_price)
    if choice is PaymentChoice.FULL:
        amount_now = rental_price
    else:
        amount_now, leftover = divmod(rental_price * payment.deposit_percent, 100)
        if leftover:
            # Cannot happen with whole-KWD prices, but never round money silently.
            raise ValueError(f"{payment.deposit_percent}% of {rental_price} fils is not a whole number of fils")
    return Amounts(
        rental_price=rental_price,
        amount_now=amount_now,
        remaining_on_day=rental_price - amount_now,
        security_deposit=kwd_to_fils(pricing.security_deposit),
    )


def kwd_to_fils(kwd: int) -> int:
    return kwd * FILS_PER_KWD


def format_kwd(fils: int) -> str:
    """25000 -> "25", 22500 -> "22.5", 22250 -> "22.25" (without the currency)."""
    whole, rest = divmod(fils, FILS_PER_KWD)
    if not rest:
        return str(whole)
    return f"{whole}.{rest:03d}".rstrip("0")
