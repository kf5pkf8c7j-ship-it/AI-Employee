"""Tests for the business info file and its loader.

Run them with:
    uv run pytest
"""

from datetime import time

import pytest

from cozysetup import show_business_info
from cozysetup.business_info import (
    DEFAULT_PATH,
    FILE_VALUES,
    BusinessInfoError,
    file_values,
    format_time_en,
    load_business_info,
    normalize_name,
)


@pytest.fixture
def info():
    return load_business_info()


def load_changed_copy(tmp_path, old, new):
    """Load a copy of the real file with `old` replaced by `new`.

    The real file is never touched. If `old` is not in the file any more, the
    test fails loudly instead of silently testing nothing.
    """
    text = DEFAULT_PATH.read_text(encoding="utf-8")
    assert old in text, f"test setup: {old!r} not found in business.toml"
    copy = tmp_path / "business.toml"
    copy.write_text(text.replace(old, new, 1), encoding="utf-8")
    return load_business_info(copy)


# --- The owner's agreed facts -------------------------------------------------
# If one of these fails after editing business.toml, either the edit was a
# mistake, or the business really changed and this test must be updated too.

def test_real_file_loads(info):
    assert info.name == "CozySetup.kw"
    assert info.timezone.key == "Asia/Kuwait"


def test_pricing(info):
    assert info.pricing.currency == "KWD"
    assert info.pricing.base_price == 50
    assert info.pricing.start_time == time(18, 0)
    assert info.pricing.end_time == time(23, 0)
    assert info.pricing.extra_hour_price == 10
    assert info.pricing.security_deposit == 20


def test_payment_rules(info):
    assert info.payment.deposit_percent == 50
    assert info.payment.deposit_min_days_ahead == 2


def test_exactly_the_six_official_locations_in_order(info):
    assert [(loc.id, loc.name.en, loc.name.ar) for loc in info.locations] == [
        ("julaia", "Julaia", "الجليعة"),
        ("bnaider", "Bnaider", "بنيدر"),
        ("sulaibikhat_desert", "Sulaibikhat Desert", "الصليبيخات"),
        ("qarayen_markets", "Qarayen Markets Area", "أسواق القرين"),
        ("al_siddeeq", "Al-Siddeeq", "الصديق"),
        ("al_subiya", "Al-Subiya", "الصبية"),
    ]


def test_arifjan_is_julaia_not_a_separate_location(info):
    owners = [
        loc.id for loc in info.locations
        if normalize_name("Arifjan") in {normalize_name(alias) for alias in loc.aliases}
    ]
    assert owners == ["julaia"]


def test_included_items(info):
    assert [item.en for item in info.included_items] == [
        "Floor seating for 8–10 people",
        "One carton of water",
        "Dallah and one carton of charcoal",
        "Tea and coffee",
        "Lighting and decorations",
        "Flashlight",
        "Wind barrier",
        "Projector",
        "Speakers",
        "Water container for washing",
        "Games",
        "Mirror",
    ]


def test_price_reply_is_the_owners_exact_wording(info):
    assert info.replies["price"].en.format(**file_values(info)) == (
        "The camping setup is 50 KWD from 6 PM to 11 PM. "
        "Each additional hour is 10 KWD, and everything is included."
    )


def test_booking_question_does_not_ask_about_extra_hours_or_people(info):
    assert info.replies["booking_question"].en == "Which location do you want? What date?"


def test_locations_reply_lists_all_locations(info):
    assert info.replies["locations"].en.format(**file_values(info)) == (
        "Our locations are:\n"
        "Julaia · Bnaider · Sulaibikhat Desert · Qarayen Markets Area · Al-Siddeeq · Al-Subiya"
    )


def test_security_deposit_policy_does_not_mention_a_limit(info):
    # The owner removed the "not a maximum limit" sentence from customer wording.
    assert "limit" not in info.policies["security_deposit"].en


# --- The loader catches mistakes ----------------------------------------------

@pytest.mark.parametrize(
    ("old", "new", "expected_problem"),
    [
        # Not valid TOML at all
        ('currency = "KWD"', 'currency = "KWD', "not valid TOML"),
        # Wrong type, missing, or in the wrong place (Piece 3a)
        ("base_price = 50", 'base_price = "fifty"', "'base_price' should be a whole number"),
        ("base_price = 50", "base_price = true", "'base_price' should be a whole number"),
        ("end_time = 23:00:00", "", "missing 'end_time'"),
        ("[setup]\n", "", "unexpected 'included_items'"),
        ('timezone = "Asia/Kuwait"', 'timezone = "Asia/Kuwiat"', "unknown timezone 'Asia/Kuwiat'"),
        # Right type, wrong business sense (Piece 3b)
        ("base_price = 50", "base_price = -50", "'base_price' must be more than 0"),
        ("end_time = 23:00:00", "end_time = 17:00:00", "must be before 'end_time'"),
        ("deposit_percent = 50", "deposit_percent = 150", "'deposit_percent' must be between 1 and 99"),
        ("deposit_min_days_ahead = 2", "deposit_min_days_ahead = 0", "must be 1 or more"),
        ('aliases = ["Bneider"', 'aliases = ["Qurain"', "'Qurain' also belongs to location 'bnaider'"),
        ('id = "al_subiya"', 'id = "julaia"', "id 'julaia' is used by more than one location"),
        ('id = "al_subiya"', 'id = "Al Subiya"', "id 'Al Subiya' must be lowercase"),
        ("[policies.weather]", "[policies.wether]", "missing the 'weather' policy"),
        ("{security_deposit} {currency} security", "{security_deposti} {currency} security",
         "unknown placeholder {security_deposti}"),
        ('en = "Which location do you want? What date?"',
         'en = "Which location do you want? What date? Ref: {reference}"',
         "[replies.booking_question]: 'en' uses unknown placeholder {reference}"),
        ("[replies.handoff]", "[replies.handof]", "missing the 'handoff' reply"),
        ('en = "Please send your name and phone number."', 'en = ""', "[replies.ask_contact]: 'en' is empty"),
    ],
)
def test_loader_rejects_mistake(tmp_path, old, new, expected_problem):
    with pytest.raises(BusinessInfoError) as error:
        load_changed_copy(tmp_path, old, new)
    assert any(expected_problem in problem for problem in error.value.problems), error.value.problems


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(BusinessInfoError, match="File not found"):
        load_business_info(tmp_path / "does-not-exist.toml")


# --- Warnings (Piece 3c) ------------------------------------------------------

def test_empty_wamd_details_is_a_warning(info):
    assert any("[payment.wamd]" in warning for warning in info.warnings())


def test_filled_wamd_details_removes_the_warning(tmp_path):
    info = load_changed_copy(tmp_path, 'details = ""', 'details = "TEST"')
    assert not any("[payment.wamd]" in warning for warning in info.warnings())


def test_missing_arabic_is_a_warning_not_an_error(info):
    assert any("No Arabic wording yet" in warning for warning in info.warnings())


def test_arabic_with_different_placeholders_is_a_warning(tmp_path):
    old = 'and everything is included."\nar = ""'
    new = 'and everything is included."\nar = "TEST {base_price} {currency}"'
    info = load_changed_copy(tmp_path, old, new)
    assert any("[replies.price]: the Arabic uses different" in warning for warning in info.warnings())


# --- Helpers ------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "shown"),
    [(time(18, 0), "6 PM"), (time(23, 0), "11 PM"), (time(23, 30), "11:30 PM"),
     (time(0, 0), "12 AM"), (time(12, 0), "12 PM"), (time(9, 5), "9:05 AM")],
)
def test_format_time_en(value, shown):
    assert format_time_en(value) == shown


def test_file_values_provides_every_file_placeholder(info):
    assert set(file_values(info)) == FILE_VALUES


def test_normalize_name_ignores_case_and_spaces():
    assert normalize_name("  QURAIN   Markets ") == "qurain markets"


# --- The show command (Piece 4) -----------------------------------------------

def test_show_command_succeeds_on_real_file(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["show_business_info"])
    assert show_business_info.main() == 0
    assert "Standard booking:  6 PM – 11 PM, 50 KWD" in capsys.readouterr().out


def test_show_command_fails_on_broken_file(tmp_path, monkeypatch, capsys):
    broken = tmp_path / "broken.toml"
    broken.write_text(DEFAULT_PATH.read_text(encoding="utf-8").replace("base_price = 50", "base_price = -50"),
                      encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["show_business_info", str(broken)])
    assert show_business_info.main() == 1
    assert "'base_price' must be more than 0" in capsys.readouterr().err
