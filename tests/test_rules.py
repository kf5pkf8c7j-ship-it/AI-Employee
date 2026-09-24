"""Tests for the booking rules (dates, payment options, amounts)."""

from datetime import date
from zoneinfo import ZoneInfo

import pytest

from cozysetup.business_info import Payment, Pricing, load_business_info
from cozysetup.rules import (
    Amounts,
    DateWindow,
    PaymentChoice,
    calculate_amounts,
    date_window,
    days_ahead,
    format_kwd,
    kwd_to_fils,
    payment_options,
    today_in,
)

# Pretend today is Monday 28 September 2026 in every test.
MONDAY = date(2026, 9, 28)
SUNDAY = date(2026, 9, 27)
TUESDAY = date(2026, 9, 29)
WEDNESDAY = date(2026, 9, 30)
NEXT_MONTH = date(2026, 10, 28)


@pytest.fixture
def info():
    return load_business_info()


# --- Dates ----------------------------------------------------------------------

def test_days_ahead():
    assert days_ahead(SUNDAY, MONDAY) == -1
    assert days_ahead(MONDAY, MONDAY) == 0
    assert days_ahead(TUESDAY, MONDAY) == 1
    assert days_ahead(WEDNESDAY, MONDAY) == 2


def test_days_ahead_across_a_month_end():
    assert days_ahead(date(2026, 10, 1), date(2026, 9, 30)) == 1


@pytest.mark.parametrize(
    ("booking_date", "window"),
    [(SUNDAY, DateWindow.PAST), (MONDAY, DateWindow.SAME_DAY),
     (TUESDAY, DateWindow.BOOKABLE), (NEXT_MONTH, DateWindow.BOOKABLE)],
)
def test_date_window(booking_date, window):
    assert date_window(booking_date, MONDAY) is window


def test_today_uses_the_business_timezone():
    # Whatever the computer's own timezone is, Kuwait's date is what counts.
    kuwait = today_in(ZoneInfo("Asia/Kuwait"))
    samoa = today_in(ZoneInfo("Pacific/Pago_Pago"))  # 14 hours behind Kuwait
    assert days_ahead(kuwait, samoa) in (0, 1)


# --- Payment options (the owner's rules) ----------------------------------------

def test_one_day_ahead_requires_full_payment(info):
    assert payment_options(TUESDAY, MONDAY, info.payment) == (PaymentChoice.FULL,)


def test_two_days_ahead_offers_deposit_or_full(info):
    assert payment_options(WEDNESDAY, MONDAY, info.payment) == (PaymentChoice.DEPOSIT, PaymentChoice.FULL)


def test_far_ahead_offers_deposit_or_full(info):
    assert payment_options(NEXT_MONTH, MONDAY, info.payment) == (PaymentChoice.DEPOSIT, PaymentChoice.FULL)


@pytest.mark.parametrize("booking_date", [MONDAY, SUNDAY])
def test_no_payment_options_for_same_day_or_past(info, booking_date):
    with pytest.raises(ValueError, match="not bookable"):
        payment_options(booking_date, MONDAY, info.payment)


def test_payment_options_follow_the_file_setting():
    three_days = Payment(deposit_percent=50, deposit_min_days_ahead=3, wamd_details="")
    assert payment_options(WEDNESDAY, MONDAY, three_days) == (PaymentChoice.FULL,)


# --- Amounts --------------------------------------------------------------------

def test_amounts_with_deposit(info):
    assert calculate_amounts(PaymentChoice.DEPOSIT, info.pricing, info.payment) == Amounts(
        rental_price=50_000, amount_now=25_000, remaining_on_day=25_000, security_deposit=20_000,
    )


def test_amounts_with_full_payment(info):
    assert calculate_amounts(PaymentChoice.FULL, info.pricing, info.payment) == Amounts(
        rental_price=50_000, amount_now=50_000, remaining_on_day=0, security_deposit=20_000,
    )


def test_deposit_of_an_odd_price_is_exact():
    pricing = Pricing(currency="KWD", base_price=45, start_time=None, end_time=None,
                      extra_hour_price=10, security_deposit=20)
    payment = Payment(deposit_percent=50, deposit_min_days_ahead=2, wamd_details="")
    amounts = calculate_amounts(PaymentChoice.DEPOSIT, pricing, payment)
    assert (amounts.amount_now, amounts.remaining_on_day) == (22_500, 22_500)


# --- Money helpers --------------------------------------------------------------

def test_kwd_to_fils():
    assert kwd_to_fils(50) == 50_000


@pytest.mark.parametrize(
    ("fils", "shown"),
    [(50_000, "50"), (25_000, "25"), (0, "0"), (22_500, "22.5"), (22_250, "22.25"), (22_125, "22.125")],
)
def test_format_kwd(fils, shown):
    assert format_kwd(fils) == shown
