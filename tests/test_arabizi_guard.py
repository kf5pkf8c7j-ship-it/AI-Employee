"""Tests for the Arabizi output guard and language detection - pretend model, no cost."""

import json
from datetime import datetime

import httpx2
import openai
import pytest

from cozysetup.agent import ARABIZI_REWRITE_INSTRUCTIONS, Agent, ConversationLog
from cozysetup.bookings import BookingService
from cozysetup.business_info import load_business_info
from cozysetup.database import Language, connect
from cozysetup.language import detect_language, has_arabic_script
from cozysetup.tools import Conversation
from tests.test_agent import PretendOpenAI, answer, call, refusal, text

INFO = load_business_info()
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=INFO.timezone)
BOOKING = {"date": "2026-10-01", "location_id": "julaia", "customer_name": "Khalid",
           "customer_phone": "66666666", "payment_choice": "deposit"}

SLIPPED = "Tam! Dizz li esmik w raqam تلفونك."
CLEAN = "Tam! Dizz li esmik w raqam telefonik."


@pytest.fixture
def service(tmp_path):
    db = connect(":memory:")
    yield BookingService(db, INFO, clock=lambda: NOW, proofs_dir=tmp_path / "proofs")
    db.close()


def make_agent(service, model, log=None):
    return Agent(model, service, Conversation("terminal", "customer-1"), log=log)


def rewrite_requests(model):
    return [r for r in model.requests if r.get("instructions") == ARABIZI_REWRITE_INSTRUCTIONS]


# --- Detecting the customer's language ------------------------------------------------

@pytest.mark.parametrize(
    ("message", "language"),
    [
        ("hala, abi a7jiz setup", Language.ARABIZI),
        ("shlonkum, 3ndkum setup? cham el si3r?", Language.ARABIZI),
        ("ee sa7", Language.ARABIZI),
        ("ismi Khalid w ra8mi 66666666", Language.ARABIZI),
        ("shlonkum", Language.ARABIZI),
        ("السلام عليكم، شكثر الكشتة؟", Language.ARABIC),
        ("Hi, can we come at 2pm on the 3rd?", None),
        ("Ahmad, 99999999", None),
        ("My booking is CS-0001", None),
        ("The 50% deposit please", None),
        ("ok", None),
        ("b Julaia yom el khamees 1 October", None),      # can't tell on its own
    ],
)
def test_detect_language(message, language):
    assert detect_language(message) is language


def test_arabic_script_includes_presentation_forms():
    assert has_arabic_script("ﷲ") and has_arabic_script("تلفونك") and not has_arabic_script("tilifunik 50 KWD")


def test_the_language_is_remembered_through_ambiguous_messages(service):
    model = PretendOpenAI(answer(text("Hala!")), answer(text("Tam")))
    agent = make_agent(service, model)
    agent.reply("hala, abi a7jiz setup")
    agent.reply("b Julaia yom el khamees 1 October")        # ambiguous: stays arabizi
    assert agent.customer_language is Language.ARABIZI


def test_the_language_follows_the_customer_if_they_switch_to_arabic(service):
    model = PretendOpenAI(answer(text("Hala!")), answer(text("هلا")))
    agent = make_agent(service, model)
    agent.reply("hala, abi a7jiz setup")
    agent.reply("ابي احجز")
    assert agent.customer_language is Language.ARABIC


def test_the_language_given_to_create_booking_is_used(service):
    model = PretendOpenAI(
        answer(call("show_booking_summary", **BOOKING)), answer(text("summary")),
        answer(call("create_booking", **BOOKING, customer_language="arabizi")), answer(text("done")),
    )
    agent = make_agent(service, model)
    agent.reply("Khalid, 66666666")          # nothing detectable here
    assert agent.customer_language is None
    agent.reply("yes")
    assert agent.customer_language is Language.ARABIZI


# --- The guard ----------------------------------------------------------------------------

def test_an_arabizi_reply_with_arabic_letters_is_rewritten(service, tmp_path):
    log = ConversationLog(tmp_path / "log.jsonl")
    model = PretendOpenAI(answer(text(SLIPPED)), answer(text(CLEAN)))
    reply = make_agent(service, model, log=log).reply("abi adfa3 el 3arboon 50%")

    assert reply.text == CLEAN
    [rewrite] = rewrite_requests(model)
    assert rewrite["input"] == [{"role": "user", "content": SLIPPED}]    # the exact reply
    assert rewrite["store"] is False and "tools" not in rewrite
    assert reply.usage.requests == 2                                    # the rewrite is counted
    guard = [json.loads(line) for line in log.path.read_text().splitlines()
             if json.loads(line)["event"] == "arabizi_guard"]
    assert guard == [{**guard[0], "outcome": "rewritten", "original": SLIPPED, "rewritten": CLEAN}]


def test_facts_are_kept_in_the_rewrite(service):
    original = ("7ajzik CS-0001 insawa w yntather el daf3.\n"
                "7awwel 25 dinar 3abr Wamd ila:\n[Tafaseel Wamd ma tجهزat للحين]\nEl raqam: +96566666666")
    fixed = ("7ajzik CS-0001 insawa w yntather el daf3.\n"
             "7awwel 25 dinar 3abr Wamd ila:\n[Tafaseel Wamd ma tjahazat lil7een]\nEl raqam: +96566666666")
    model = PretendOpenAI(answer(text(original)), answer(text(fixed)))
    assert make_agent(service, model).reply("ee sa7").text == fixed


def test_a_rewrite_may_spell_arabizi_words_differently(service):
    original = "7awwel 25 dinar 3abr Wamd, shكراً"
    respelled = "Hawwel 25 dinar 3an tareeq Wamd, shukran"     # "7awwel" -> "Hawwel": not a fact
    model = PretendOpenAI(answer(text(original)), answer(text(respelled)))
    assert make_agent(service, model).reply("ee sa7").text == respelled


def test_no_rewrite_when_the_arabizi_reply_is_already_clean(service):
    model = PretendOpenAI(answer(text(CLEAN)))
    reply = make_agent(service, model).reply("abi adfa3 el 3arboon 50%")
    assert reply.text == CLEAN
    assert rewrite_requests(model) == []


def test_english_customers_are_never_rewritten(service):
    reply_with_arabic = "We set up in Julaia (الجليعة)."
    model = PretendOpenAI(answer(text(reply_with_arabic)))
    assert make_agent(service, model).reply("Where do you set up?").text == reply_with_arabic
    assert rewrite_requests(model) == []


def test_arabic_customers_are_never_rewritten(service):
    model = PretendOpenAI(answer(text("الكشتة بـ50 دينار")))
    assert make_agent(service, model).reply("شكثر الكشتة؟").text == "الكشتة بـ50 دينار"
    assert rewrite_requests(model) == []


@pytest.mark.parametrize(
    ("rewrite", "reason"),
    [
        (answer(text("Tam! Dizz li esmik w raqam تليفونك.")), "the rewrite still contains Arabic letters"),
        (answer(text("7awwel dinar 3abr Wamd")), "the rewrite lost numbers: ['25']"),
        (answer(refusal()), "the rewrite request did not complete"),
        (answer(status="incomplete", incomplete_reason="max_output_tokens"), "the rewrite request did not complete"),
        (openai.APIConnectionError(request=httpx2.Request("POST", "https://api.openai.com")), "APIConnectionError"),
    ],
    ids=["still arabic", "lost a number", "refused", "cut off", "api error"],
)
def test_a_bad_rewrite_is_not_used_and_the_original_is_sent(service, tmp_path, rewrite, reason):
    original = "7awwel 25 dinar 3abr Wamd. Tam, شكراً"
    log = ConversationLog(tmp_path / "log.jsonl")
    model = PretendOpenAI(answer(text(original)), rewrite)
    reply = make_agent(service, model, log=log).reply("abi adfa3 el 3arboon 50%")

    assert reply.text == original
    assert not reply.handed_off_after_problem            # a failed rewrite is not a problem handoff
    assert service.list_handoffs() == []
    [guard] = [json.loads(line) for line in log.path.read_text().splitlines()
               if json.loads(line)["event"] == "arabizi_guard"]
    assert guard["outcome"] == "kept original" and reason in guard["reason"]


def test_only_one_rewrite_attempt_per_reply(service):
    model = PretendOpenAI(answer(text(SLIPPED)), answer(text(SLIPPED)))
    make_agent(service, model).reply("ee sa7")
    assert len(rewrite_requests(model)) == 1


def test_the_guard_does_not_change_the_booking_flow(service):
    model = PretendOpenAI(
        answer(call("show_booking_summary", **BOOKING)), answer(text("Raja3 tafaseel 7ajzik")),
        answer(call("create_booking", **BOOKING, customer_language="arabizi")),
        answer(text("7ajzik CS-0001 insawa. 7awwel 25 dinar للحين")),
        answer(text("7ajzik CS-0001 insawa. 7awwel 25 dinar lil7een")),
    )
    agent = make_agent(service, model)
    agent.reply("ismi Khalid w ra8mi 66666666")
    reply = agent.reply("ee sa7")
    assert reply.text == "7ajzik CS-0001 insawa. 7awwel 25 dinar lil7een"
    booking = service.get_booking("CS-0001")
    assert (booking.status.value, booking.language) == ("pending_payment", Language.ARABIZI)
    assert reply.tools_used == ["create_booking"]
