"""Fill in the owner's fixed replies from business.toml with real values.

The AI never types facts like amounts, dates or references itself: the tools
hand it the finished reply text produced here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cozysetup.business_info import BusinessInfo, file_values, format_date_en
from cozysetup.rules import Amounts, PaymentChoice, calculate_amounts, format_kwd

if TYPE_CHECKING:   # only for type hints - importing bookings here would create an import loop
    from cozysetup.bookings import Booking, BookingPreview

WAMD_NOT_SET = "[Wamd details not set up yet - the owner will send them]"


def render(info: BusinessInfo, reply: str, values: dict[str, object] | None = None) -> dict[str, str]:
    """A reply in each language the owner has approved: {"en": ..., "ar": ...}.

    "ar" is only included once the owner has written the Arabic wording.
    """
    filled = file_values(info)
    filled["wamd_details"] = filled["wamd_details"] or WAMD_NOT_SET
    filled.update(values or {})
    text = info.replies[reply]
    result = {"en": text.en.format(**filled).strip()}
    if text.ar.strip():
        arabic = dict(filled)
        if "location" in arabic:
            arabic["location"] = _arabic_location_name(info, str(arabic["location"]))
        result["ar"] = text.ar.format(**arabic).strip()
    return result


def amount_values(info: BusinessInfo, choice: PaymentChoice, amounts: Amounts) -> dict[str, object]:
    """{rental_price}, {amount_now}, {deposit_amount}, {remaining_amount}, {due_on_day}."""
    values = {
        "rental_price": format_kwd(amounts.rental_price),
        "amount_now": format_kwd(amounts.amount_now),
        "remaining_amount": format_kwd(amounts.remaining_on_day),
        "deposit_amount": format_kwd(calculate_amounts(PaymentChoice.DEPOSIT, info.pricing, info.payment).amount_now),
    }
    fragment = "due_on_day_after_deposit" if choice is PaymentChoice.DEPOSIT else "due_on_day_after_full_payment"
    values["due_on_day"] = render(info, fragment, values)["en"]
    return values


def booking_values(info: BusinessInfo, booking: Booking | BookingPreview) -> dict[str, object]:
    """Every {placeholder} a reply about this booking (or preview) may use."""
    values = {
        "location": location_name(info, booking.location_id),
        "date": format_date_en(booking.booking_date),
        "name": booking.customer_name,
        "phone": booking.customer_phone,
        **amount_values(info, booking.payment_choice, booking.amounts),
    }
    reference = getattr(booking, "reference", None)   # a preview has no reference yet
    if reference:
        values["reference"] = reference
    return values


def location_name(info: BusinessInfo, location_id: str) -> str:
    return next(location.name.en for location in info.locations if location.id == location_id)


def _arabic_location_name(info: BusinessInfo, english_name: str) -> str:
    return next((l.name.ar for l in info.locations if l.name.en == english_name), english_name)
