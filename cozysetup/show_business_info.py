"""Print the business information the way a person would read it.

Run it with:
    uv run python -m cozysetup.show_business_info
    uv run python -m cozysetup.show_business_info path/to/other.toml
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from cozysetup.business_info import (
    DEFAULT_PATH,
    BusinessInfo,
    BusinessInfoError,
    file_values,
    load_business_info,
)

NOT_PROVIDED = "(not provided yet)"


class _KeepUnfilled(dict):
    """Fill in the placeholders we know; leave booking ones like {reference} visible."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def show(info: BusinessInfo) -> str:
    values = file_values(info)
    values["wamd_details"] = values["wamd_details"] or f"[Wamd details {NOT_PROVIDED}]"

    def fill(wording: str) -> str:
        return wording.format_map(_KeepUnfilled(values))

    pricing, payment, currency = info.pricing, info.payment, info.pricing.currency
    deposit = Decimal(pricing.base_price) * payment.deposit_percent / 100
    lines: list[str] = []

    def heading(title: str) -> None:
        lines.extend(["", title.upper(), "-" * len(title)])

    lines.append(f"{info.name}  (all dates and times: {info.timezone.key})")

    heading("Pricing")
    lines.append(f"Standard booking:  {values['start_time']} – {values['end_time']}, {pricing.base_price} {currency}")
    lines.append(f"Extra hour:        {pricing.extra_hour_price} {currency} (quoted only - the owner handles extra hours)")
    lines.append(f"Security deposit:  {pricing.security_deposit} {currency} (refundable, paid on the booking day)")

    heading("Payment")
    lines.append(f"Booked for today:  handed to the owner (if the owner accepts: 100% now, {pricing.base_price} {currency})")
    if payment.deposit_min_days_ahead > 1:
        last_full_day = payment.deposit_min_days_ahead - 1
        days = "1 day" if last_full_day == 1 else f"1–{last_full_day} days"
        lines.append(f"Booked {days} ahead: 100% now ({pricing.base_price} {currency})")
    lines.append(
        f"Booked {payment.deposit_min_days_ahead}+ days ahead: customer chooses 100% "
        f"({pricing.base_price} {currency}) or {payment.deposit_percent}% ({deposit} {currency})"
    )
    lines.append(f"Wamd details:      {payment.wamd_details or NOT_PROVIDED}")

    heading("Reminders")
    lines.append(f"Confirmed bookings get a reminder {info.reminders.hours_before_start} hours before "
                 f"{values['start_time']} (none if confirmed after that).")

    heading(f"Locations ({len(info.locations)})")
    for number, location in enumerate(info.locations, start=1):
        lines.append(f"{number}. {location.name.en} — {location.name.ar}   [id: {location.id}]")
        if location.aliases:
            lines.append(f"   also recognised as: {', '.join(location.aliases)}")

    heading(f"Included in the setup ({len(info.included_items)} items)")
    for item in info.included_items:
        lines.append(f"- {item.en}   | ar: {item.ar or NOT_PROVIDED}")

    heading("Policies (English)")
    for name, text in info.policies.items():
        lines.append(f"{name}:")
        lines.append(f"  {fill(text.en)}")

    heading("Languages")
    lines.append("English and Kuwaiti Arabic: wording from the file, word for word.")
    lines.append("Arabizi: written by the AI from the Arabic, following this rule:")
    lines.append(f"  {info.arabizi_rule}")

    heading("Replies (English)")
    lines.append("Values like {reference} are filled in from each booking.")
    for name, text in info.replies.items():
        lines.append(f"[{name}]")
        lines.extend(f"  {line}" for line in fill(text.en).strip().splitlines())

    warnings = info.warnings()
    heading(f"Warnings ({len(warnings)})")
    lines.extend(f"! {warning}" for warning in warnings or ["None - everything is filled in."])

    return "\n".join(lines)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    try:
        info = load_business_info(path)
    except BusinessInfoError as error:
        print(error, file=sys.stderr)
        return 1
    print(show(info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
