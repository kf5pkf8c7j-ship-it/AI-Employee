"""Tests for the AI employee's tools - run directly, no AI involved."""

import json
from dataclasses import replace
from datetime import date, datetime

import pytest

from cozysetup.bookings import BookingService
from cozysetup.business_info import Text, load_business_info
from cozysetup.database import BookingStatus, HandoffType, connect
from cozysetup.replies import render
from cozysetup.tools import Conversation, Tools, tool_definitions

INFO = load_business_info()
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=INFO.timezone)   # Monday 2 PM, Kuwait
JPG = b"\xff\xd8\xff\xe0" + bytes(100)

BOOKING = {
    "date": "2026-10-01", "location_id": "julaia", "customer_name": "Ahmad",
    "customer_phone": "9999 9999", "payment_choice": "deposit",
}


@pytest.fixture
def service(tmp_path):
    db = connect(":memory:")
    yield BookingService(db, INFO, clock=lambda: NOW, proofs_dir=tmp_path / "proofs")
    db.close()


@pytest.fixture
def conversation():
    return Conversation(channel="terminal", channel_user_id="test-customer")


@pytest.fixture
def tools(service, conversation):
    return Tools(service, conversation)


def call(tools, name, **tool_input):
    result = tools.run(name, tool_input)
    return json.loads(result.content), result.is_error


def summary_then_create(tools, **changes):
    details = {**BOOKING, **changes}
    call(tools, "show_booking_summary", **details)
    return call(tools, "create_booking", **details)


# --- The definitions Claude reads ---------------------------------------------------

def test_six_strict_tools():
    definitions = tool_definitions(INFO)
    assert [tool["name"] for tool in definitions] == [
        "check_availability", "show_booking_summary", "create_booking", "attach_payment_proof",
        "get_booking_status", "handoff_to_human",
    ]
    for tool in definitions:
        schema = tool["input_schema"]
        assert tool["strict"] is True
        assert schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"])
        assert tool["description"]


def test_locations_are_limited_to_the_six_ids():
    create = tool_definitions(INFO)[2]
    assert create["input_schema"]["properties"]["location_id"]["enum"] == [
        "julaia", "bnaider", "sulaibikhat_desert", "qarayen_markets", "al_siddeeq", "al_subiya",
    ]


def test_the_ai_cannot_create_date_conflict_handoffs():
    handoff = tool_definitions(INFO)[5]
    assert "date_conflict" not in handoff["input_schema"]["properties"]["type"]["enum"]


def test_definitions_never_change_between_calls():
    # Identical every time, so they can be cached.
    assert json.dumps(tool_definitions(INFO)) == json.dumps(tool_definitions(INFO))


def test_nothing_the_ai_sends_can_confirm_a_booking():
    for tool in tool_definitions(INFO):
        assert not any("confirm" in name for name in tool["input_schema"]["properties"])


def test_the_ai_cannot_set_who_the_customer_is():
    for tool in tool_definitions(INFO):
        assert "channel" not in tool["input_schema"]["properties"]
        assert "channel_user_id" not in tool["input_schema"]["properties"]


# --- check_availability ----------------------------------------------------------------

def test_available_two_days_ahead_gives_the_owners_payment_choice_reply(tools):
    result, is_error = call(tools, "check_availability", date="2026-10-01")
    assert not is_error
    assert result["status"] == "available"
    assert result["date"] == "Thursday 1 October 2026"
    assert result["reply"]["en"] == (
        "You can pay the full amount (50 KWD) now, or a 50% deposit (25 KWD) now "
        "and the remaining 25 KWD on the booking day. Which do you prefer?"
    )
    assert [o["choice"] for o in result["payment_options"]] == ["deposit", "full"]
    assert result["payment_options"][0]["pay_on_booking_day"] == "25 KWD remaining + 20 KWD security deposit"


def test_tomorrow_offers_full_payment_only(tools):
    result, _ = call(tools, "check_availability", date="2026-09-29")
    assert [o["choice"] for o in result["payment_options"]] == ["full"]
    assert result["payment_options"][0]["pay_now_via_wamd"] == "50 KWD"
    assert "reply" not in result


def test_same_day_points_to_handoff(tools):
    result, _ = call(tools, "check_availability", date="2026-09-28")
    assert result["status"] == "same_day"
    assert "handoff_to_human" in result["instruction"]


def test_past_date(tools):
    assert call(tools, "check_availability", date="2026-09-27")[0]["status"] == "past"


def test_taken_date_gives_the_owners_unavailable_reply(tools, service):
    summary_then_create(tools)
    service.approve_payment("CS-0001")
    result, _ = call(tools, "check_availability", date="2026-10-01")
    assert result["status"] == "taken"
    assert result["reply"]["en"] == ("Sorry, the setup is not available on Thursday 1 October 2026. "
                                     "Please choose another date.")


def test_nonsense_date_is_an_error(tools):
    result, is_error = call(tools, "check_availability", date="next thursday")
    assert is_error and "YYYY-MM-DD" in result["error"]


# --- show_booking_summary, then create_booking ------------------------------------

def test_summary_saves_nothing_and_returns_the_owners_summary(tools, service):
    result, is_error = call(tools, "show_booking_summary", **BOOKING)
    assert not is_error and result["saved"] is False
    summary = result["reply"]["en"]
    assert "Date: Thursday 1 October 2026, 6 PM – 11 PM" in summary
    assert "Pay now via Wamd: 25 KWD" in summary
    assert "Pay on the booking day: 25 KWD remaining + 20 KWD security deposit" in summary
    assert "Phone: +96599999999" in summary
    assert service.list_bookings() == []


def test_create_without_a_summary_is_refused(tools, service):
    result, is_error = call(tools, "create_booking", **BOOKING)
    assert is_error and result["saved"] is False
    assert "show_booking_summary" in result["instruction"]
    assert service.list_bookings() == []


def test_create_with_details_different_from_the_summary_is_refused(tools, service):
    call(tools, "show_booking_summary", **BOOKING)
    result, is_error = call(tools, "create_booking", **{**BOOKING, "payment_choice": "full"})
    assert is_error and "has not seen a summary with these details" in result["error"]
    assert service.list_bookings() == []


def test_same_phone_written_differently_still_matches_the_summary(tools):
    call(tools, "show_booking_summary", **BOOKING)
    result, is_error = call(tools, "create_booking", **{**BOOKING, "customer_phone": "+965 99999999"})
    assert not is_error and result["saved"] is True


def test_create_makes_a_pending_payment_booking_never_a_confirmed_one(tools, service):
    result, is_error = summary_then_create(tools)
    assert not is_error and result["saved"] is True and result["reference"] == "CS-0001"
    assert result["reply"]["en"].startswith("Your booking CS-0001 has been created and is pending payment.")
    assert "Please transfer 25 KWD via Wamd to:" in result["reply"]["en"]
    booking = service.get_booking("CS-0001")
    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert (booking.channel, booking.channel_user_id) == ("terminal", "test-customer")   # set by our code


def test_create_twice_cannot_make_a_second_booking(tools, service):
    summary_then_create(tools)
    result, is_error = call(tools, "create_booking", **BOOKING)
    assert is_error
    assert len(service.list_bookings()) == 1


@pytest.mark.parametrize(
    ("changes", "refused", "hint"),
    [
        ({"date": "2026-09-28", "payment_choice": "full"}, "same_day", "handoff_to_human"),
        ({"customer_phone": "123"}, "invalid_phone", "country code"),
        ({"date": "2026-09-29"}, "payment_choice_not_allowed", "check_availability"),
    ],
)
def test_refusals_come_back_with_an_instruction(tools, service, changes, refused, hint):
    result, is_error = call(tools, "show_booking_summary", **{**BOOKING, **changes})
    assert is_error
    assert result["refused"] == refused
    assert hint in result["instruction"]
    assert service.list_bookings() == []


# --- attach_payment_proof ---------------------------------------------------------------

def test_screenshot_is_saved_and_the_owners_reply_returned(tools, service, conversation):
    summary_then_create(tools)
    number = conversation.add_attachment(JPG)
    result, is_error = call(tools, "attach_payment_proof", reference="CS-0001",
                            customer_phone="99999999", attachment_number=number)
    assert not is_error
    assert result["reply"]["en"].startswith("Thank you, we've received your screenshot.")
    assert service.get_booking("CS-0001").status is BookingStatus.PAYMENT_SUBMITTED


def test_screenshot_that_was_never_sent(tools):
    summary_then_create(tools)
    result, is_error = call(tools, "attach_payment_proof", reference="CS-0001",
                            customer_phone="99999999", attachment_number=3)
    assert is_error and "has not sent image #3" in result["error"]


def test_screenshot_with_wrong_phone(tools, conversation):
    summary_then_create(tools)
    number = conversation.add_attachment(JPG)
    result, is_error = call(tools, "attach_payment_proof", reference="CS-0001",
                            customer_phone="55555555", attachment_number=number)
    assert is_error and result["refused"] == "booking_not_found"


# --- get_booking_status ----------------------------------------------------------------

def test_status_not_found(tools):
    result, _ = call(tools, "get_booking_status", reference="CS-0404", customer_phone="99999999")
    assert result["found"] is False


def test_status_of_a_pending_booking(tools):
    summary_then_create(tools)
    result, _ = call(tools, "get_booking_status", reference="cs-0001", customer_phone="+965 9999 9999")
    assert result["status"] == "pending_payment"
    assert "not secured yet" in result["status_meaning"]


def test_status_of_a_confirmed_booking_uses_the_owners_reply(tools, service):
    summary_then_create(tools)
    service.approve_payment("CS-0001")
    result, _ = call(tools, "get_booking_status", reference="CS-0001", customer_phone="99999999")
    assert result["reply"]["en"].startswith("Your booking CS-0001 is confirmed!")


def test_a_flagged_booking_reveals_nothing_but_the_owners_reply(tools, service):
    summary_then_create(tools)                                              # CS-0001
    summary_then_create(tools, customer_name="Sara", customer_phone="55555555")  # CS-0002
    service.approve_payment("CS-0002")
    result, _ = call(tools, "get_booking_status", reference="CS-0001", customer_phone="99999999")
    assert set(result) == {"found", "reply", "instruction"}
    assert result["reply"]["en"] == "The owner will contact you about your booking."


# --- handoff_to_human -------------------------------------------------------------------

def test_handoff_returns_the_owners_reply_and_records_the_channel(tools, service):
    result, _ = call(tools, "handoff_to_human", type="weather", summary="Asks if Saturday will be windy",
                     customer_name="Khalid", customer_phone=None, booking_reference=None)
    assert result["reply"]["en"] == "I've passed your request to the owner, who will get back to you."
    [handoff] = service.list_handoffs()
    assert handoff.type is HandoffType.WEATHER
    assert (handoff.channel, handoff.channel_user_id) == ("terminal", "test-customer")


def test_same_day_handoff_uses_the_same_day_reply(tools):
    result, _ = call(tools, "handoff_to_human", type="same_day", summary="Wants tonight at Julaia",
                     customer_name=None, customer_phone=None, booking_reference=None)
    assert result["reply"]["en"].startswith("Same-day bookings are handled by the owner directly.")


def test_ai_cannot_create_a_date_conflict_handoff(tools, service):
    _, is_error = call(tools, "handoff_to_human", type="date_conflict", summary="x",
                       customer_name=None, customer_phone=None, booking_reference=None)
    assert is_error and service.list_handoffs() == []


# --- Other ---------------------------------------------------------------------------------

def test_unknown_tool(tools):
    result, is_error = call(tools, "approve_payment", reference="CS-0001")
    assert is_error and "Unknown tool" in result["error"]


def test_missing_input_is_an_error_not_a_crash(tools):
    result, is_error = call(tools, "check_availability")
    assert is_error and "Invalid input" in result["error"]


def test_arabic_is_included_once_the_owner_writes_it():
    replies = dict(INFO.replies)
    replies["date_unavailable"] = Text(en=INFO.replies["date_unavailable"].en, ar="TEST {date}")
    info = replace(INFO, replies=replies)
    assert render(info, "date_unavailable", {"date": "X"}) == {
        "en": "Sorry, the setup is not available on X. Please choose another date.", "ar": "TEST X",
    }
    assert "ar" not in render(INFO, "date_unavailable", {"date": "X"})   # empty -> not included
