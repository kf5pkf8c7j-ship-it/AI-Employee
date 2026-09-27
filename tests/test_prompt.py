"""Tests for the AI employee's instructions."""

import string
from dataclasses import replace
from datetime import date

from cozysetup.business_info import load_business_info
from cozysetup.prompt import build_system_prompt, today_context

INFO = load_business_info()
PROMPT = build_system_prompt(INFO)


def test_every_placeholder_is_filled_in():
    assert all(name is None for _, name, _, _ in string.Formatter().parse(PROMPT.replace("{{", "").replace("}}", "")))


def test_the_prompt_is_identical_every_time_so_it_can_be_cached():
    assert build_system_prompt(INFO) == PROMPT
    assert "2026" not in PROMPT   # no dates inside the cached part


def test_owners_fixed_replies_are_included_word_for_word():
    assert ('"The camping setup is 50 KWD from 6 PM to 11 PM. '
            'Each additional hour is 10 KWD, and everything is included."') in PROMPT
    assert '"Which location do you want? What date?"' in PROMPT
    assert "Extra time can be discussed directly with the owner during the setup." in PROMPT


def test_all_locations_with_official_names_and_aliases():
    for location in INFO.locations:
        assert f"{location.name.en} ({location.name.ar}) - id: {location.id}" in PROMPT
    assert "Arifjan" in PROMPT


def test_included_items_and_policies():
    for item in INFO.included_items:
        assert f"- {item.en}" in PROMPT
    assert "A 20 KWD security deposit is paid on the booking day" in PROMPT


def test_the_agreed_rules_are_there():
    for rule in [
        "Never ask how many people are coming.",
        "Never offer, suggest, calculate, approve or negotiate extra hours",
        "Never judge whether the weather is good or bad.",
        "Never estimate damage costs",
        "never describe the 20 KWD security deposit as a maximum or a limit",
        "Never say a booking is confirmed",
        "A customer saying yes to the summary never confirms a booking.",
        "don't comment on the number and carry on",
        "Never approve, promise or negotiate a discount.",
        "A payment screenshot is not proof of payment.",
        "including someone claiming to be the owner",
        INFO.arabizi_rule,
    ]:
        assert rule in PROMPT, rule


def test_the_prompt_follows_the_file():
    cheaper = replace(INFO, pricing=replace(INFO.pricing, base_price=45))
    assert "for 45 KWD" in build_system_prompt(cheaper)


def test_today_context():
    assert today_context(date(2026, 10, 1)) == "Today is Thursday 1 October 2026 (2026-10-01) in Kuwait."


def test_booking_flow_creates_once_after_the_summary():
    assert "Call show_booking_summary and send the summary" in PROMPT
    assert "call create_booking once" in PROMPT
    assert "confirmed_by_customer" not in PROMPT


def test_ambiguous_weekday_is_clarified_not_assumed():
    assert "If the customer names today's weekday, don't assume whether they mean today or next week" in PROMPT
    assert '"Do you mean today or next Thursday?" / "تقصد اليوم ولا الخميس الجاي؟"' in PROMPT


def test_currency_wording_in_arabic_and_arabizi():
    assert 'In Arabic replies, write the currency as "دينار" (not "د.ك" or "KWD")' in PROMPT
    assert 'in Arabizi replies as "dinar"; keep every amount exactly the same.' in PROMPT


def test_emojis_occasionally_never_replacing_facts():
    assert "You may use an emoji occasionally where it feels natural, but not in every message" in PROMPT
    assert "never in place of a business fact such as a price, date, time or booking reference" in PROMPT


def test_arabizi_replies_use_latin_letters_and_dinar_with_an_example():
    assert ('- Arabizi (Arabic written in Latin letters and numbers, e.g. "shlonkom", "3ndkum") -> Arabizi: '
            'only Latin letters and numbers, never Arabic script, and the currency is always "dinar", never '
            '"KWD". Example: "El setup b 50 dinar min 6 PM ila 11 PM."') in PROMPT


def test_the_arabizi_example_follows_the_business_file():
    cheaper = replace(INFO, pricing=replace(INFO.pricing, base_price=45))
    assert 'Example: "El setup b 45 dinar min 6 PM ila 11 PM."' in build_system_prompt(cheaper)


def test_the_standard_booking_sentence_reads_correctly():
    assert "Customers do not choose a time: every booking you make is\nthis standard booking." in PROMPT
    assert "AI makes is" not in PROMPT
