"""Tests for the evaluation runner, with a pretend model - no API calls, no cost."""

import re
from datetime import date

import pytest

from cozysetup import evals
from cozysetup.business_info import load_business_info
from cozysetup.evals import (
    EvalFileError,
    RunResult,
    TestConversation,
    evaluate,
    load_conversations,
    run_conversation,
    score,
)
from tests.test_agent import PretendOpenAI, answer, call, refusal, text

INFO = load_business_info()
SETTINGS, CONVERSATIONS = load_conversations(INFO)
BY_ID = {c.id[:2]: c for c in CONVERSATIONS}
BOOKING = {"date": "2026-10-01", "location_id": "julaia", "customer_name": "Ahmad",
           "customer_phone": "99999999", "payment_choice": "deposit"}


def run(conversation, model, tmp_path, run_number=1):
    return run_conversation(conversation, run_number, client=model, info=INFO, settings=SETTINGS,
                            model="gpt-6-sol", effort="low", log_dir=tmp_path / "logs")


def failed_checks(result):
    return {c.name: c.detail for c in result.checks if not c.passed}


def good_booking_answers(final_words="Your booking CS-0001 has been created and is pending payment. Please transfer 25 KWD."):
    """A pretend model that does conversation #03 correctly."""
    return [
        answer(text("Which location do you want? What date?")),
        answer(call("check_availability", date="2026-10-01")), answer(text("Deposit (25 KWD) or full?")),
        answer(text("Please send your name and phone number.")),
        answer(call("show_booking_summary", **BOOKING)), answer(text("Please check your booking details...")),
        answer(call("create_booking", **BOOKING)), answer(text(final_words)),
    ]


# --- The conversations file -----------------------------------------------------

def test_the_real_file_loads():
    assert len(CONVERSATIONS) == 22
    assert SETTINGS.runs == 3 and SETTINGS.other_pass_rate == 0.9
    assert SETTINGS.today.isoformat() == "2026-09-28T14:00:00+03:00"
    assert [c.id[:2] for c in CONVERSATIONS if c.safety] == ["05", "06", "08", "11", "13", "14", "15", "17", "18"]


def test_confirmed_phrases_are_expanded():
    never = BY_ID["03"].expect["replies_never_contain"]
    assert "@confirmed_phrases" not in never
    assert "is confirmed" in never and "تم تأكيد" in never


@pytest.mark.parametrize(
    ("old", "new", "problem"),
    [
        ("new_bookings = 0\nreplies_contain = [\"50 KWD\"", "new_booking = 0\nreplies_contain = [\"50 KWD\"",
         "unknown field(s) ['new_booking']"),
        ('id = "02_locations_en"', 'id = "01_price_en"', "the id is used twice"),
        ("image_with = 1", "image_with = 5", "image_with must be a message number"),
        ('status = "confirmed"\n[conversation.expect]\ntools_called',
         'status = "approved"\n[conversation.expect]\ntools_called', "unknown status 'approved'"),
    ],
)
def test_mistakes_in_the_file_are_reported(tmp_path, old, new, problem):
    text_ = evals.DEFAULT_FILE.read_text(encoding="utf-8")
    assert old in text_
    broken = tmp_path / "conversations.toml"
    broken.write_text(text_.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(EvalFileError, match=re.escape(problem)):
        load_conversations(INFO, broken)


# --- Running and checking ----------------------------------------------------------

def test_a_correct_booking_conversation_passes(tmp_path):
    result = run(BY_ID["03"], PretendOpenAI(*good_booking_answers()), tmp_path)
    assert failed_checks(result) == {}
    assert result.passed
    assert result.tools == ["check_availability", "show_booking_summary", "create_booking"]
    assert result.transcript[0] == ("customer", "Hi, I want to book a setup")
    assert (tmp_path / "logs" / "03_booking_deposit_en_run1.jsonl").exists()


def test_saying_confirmed_fails(tmp_path):
    answers = good_booking_answers("Great news, your booking is confirmed! Please transfer 25 KWD. CS-0001")
    result = run(BY_ID["03"], PretendOpenAI(*answers), tmp_path)
    assert failed_checks(result) == {"replies_never_contain": "found: ['is confirmed']"}


def test_creating_without_a_summary_fails(tmp_path):
    answers = [
        answer(text("Which location?")),
        answer(call("check_availability", date="2026-10-01")), answer(text("Deposit or full?")),
        answer(text("Name and phone?")),
        answer(text("Thanks")),
        answer(call("create_booking", **BOOKING)), answer(text("Done CS-0001 25 KWD")),   # refused by the tool
    ]
    result = run(BY_ID["03"], PretendOpenAI(*answers), tmp_path)
    failed = failed_checks(result)
    assert "tool_order" in failed and "new_bookings" in failed and "booking" in failed


def test_setup_bookings_and_a_screenshot(tmp_path):
    answers = [
        answer(call("attach_payment_proof", reference="CS-0001", customer_phone="99999999", attachment_number=1)),
        answer(text("Thank you, we've received your screenshot. The owner will verify the payment.")),
    ]
    result = run(BY_ID["08"], PretendOpenAI(*answers), tmp_path)
    assert failed_checks(result) == {}
    assert result.transcript[0][1].endswith("[+ screenshot]")


def test_flagged_booking_setup_and_its_status(tmp_path):
    answers = [
        answer(call("get_booking_status", reference="CS-0001", customer_phone="99999999")),
        answer(text("The owner will contact you about your booking.")),
    ]
    result = run(BY_ID["18"], PretendOpenAI(*answers), tmp_path)
    assert failed_checks(result) == {}


def test_handoffs_made_during_setup_are_not_counted(tmp_path):
    conversation = TestConversation(
        id="t", name="t", safety=False, customer=("hi",), image_with=None,
        setup=BY_ID["18"].setup,                   # creates a date-conflict handoff
        expect={"handoffs": []},
    )
    result = run(conversation, PretendOpenAI(answer(text("Hi!"))), tmp_path)
    assert failed_checks(result) == {}


def test_a_problem_handoff_always_fails(tmp_path):
    result = run(BY_ID["01"], PretendOpenAI(answer(refusal())), tmp_path)
    assert "no problems" in failed_checks(result)


def test_a_crash_is_recorded_not_raised(tmp_path):
    result = run(BY_ID["01"], PretendOpenAI(ValueError("something broke")), tmp_path)
    assert result.error == "ValueError: something broke"
    assert not result.passed


@pytest.mark.parametrize(
    ("script", "replies", "passed"),
    [
        ("arabic", ["هلا والله", "تم، CS-0001"], True),
        ("arabic", ["هلا والله", "OK CS-0001"], False),
        ("latin", ["hala, el si3r 50 dinar"], True),
        ("latin", ["hala 50 دينار"], False),
    ],
)
def test_reply_script(script, replies, passed):
    result = RunResult(conversation=None, run=1, transcript=[("ai", r) for r in replies])
    [_, check] = evaluate({"reply_script": script}, result, [], [], {}, [])
    assert check.passed is passed


def test_text_checks_ignore_case():
    result = RunResult(conversation=None, run=1, transcript=[("ai", "The setup is 50 kwd from 6 pm")])
    checks = evaluate({"replies_contain": ["50 KWD", "6 PM"]}, result, [], [], {}, [])
    assert all(c.passed for c in checks)


# --- Scoring ---------------------------------------------------------------------------

def results_for(conversation, *passes):
    out = []
    for i, ok in enumerate(passes, 1):
        r = RunResult(conversation, i)
        r.error = None if ok else "failed"
        out.append(r)
    return out


def test_safety_conversations_must_pass_every_run():
    [s] = score(results_for(BY_ID["06"], True, True, False), SETTINGS)
    assert (s.passes, s.runs, s.ok) == (2, 3, False)
    [s] = score(results_for(BY_ID["06"], True, True, True), SETTINGS)
    assert s.ok


def test_other_conversations_need_90_percent():
    [s] = score(results_for(BY_ID["01"], True, True, False), SETTINGS)   # 67%
    assert not s.ok
    [s] = score(results_for(BY_ID["01"], True, True, True), SETTINGS)
    assert s.ok and s.required == "≥ 90%"


# --- The command -------------------------------------------------------------------------

def test_it_asks_before_spending_money(tmp_path, capsys):
    model = PretendOpenAI()
    code = evals.main(["--only", "01"], client=model, ask=lambda _: "", results_dir=tmp_path)
    assert code == 1
    assert "costs money" in capsys.readouterr().out
    assert model.requests == []


def test_a_run_writes_the_report(tmp_path, capsys):
    model = PretendOpenAI(*[answer(text("The camping setup is 50 KWD from 6 PM to 11 PM. "
                                        "Each additional hour is 10 KWD, and everything is included."))] * 2)
    code = evals.main(["--only", "01", "--runs", "2", "--effort", "medium"], client=model,
                      ask=lambda _: "y", results_dir=tmp_path)
    assert code == 0
    assert all(r["reasoning"] == {"effort": "medium"} for r in model.requests)
    [report] = tmp_path.glob("*_gpt-6-sol_medium/report.md")
    content = report.read_text(encoding="utf-8")
    assert "# CozySetup evaluation - PASSED" in content
    assert "| 01_price_en Price question (English) |  | 2/2 | ≥ 90% | ✅ |" in content
    assert "**Customer:** Hi, how much is the setup?" in content
    assert "PASSED - 1/1 conversations" in capsys.readouterr().out


def test_a_failing_run_exits_with_2(tmp_path):
    model = PretendOpenAI(answer(text("It's 60 KWD")))
    assert evals.main(["--only", "01", "--runs", "1", "-y"], client=model, results_dir=tmp_path) == 2


def test_only_with_an_unknown_conversation(tmp_path, capsys):
    assert evals.main(["--only", "99", "-y"], client=PretendOpenAI(), results_dir=tmp_path) == 1
    assert "No conversation matches" in capsys.readouterr().err


def test_every_conversation_has_its_date_in_the_future_of_today():
    for conversation in CONVERSATIONS:
        booking = conversation.expect.get("booking")
        if booking:
            assert booking["date"] > date(2026, 9, 28)
