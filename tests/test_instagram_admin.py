"""Tests for the owner's Instagram admin commands (Step 7.6) - made-up accounts,
temporary databases, pretend AI and Instagram."""

import pytest

from cozysetup import admin, outbox
from cozysetup.conversations import readable_history
from cozysetup.senders import SendError
from tests.test_agent import BOOKING, PretendOpenAI, answer, call, text
from tests.test_instagram_outbox import PretendInstagram
from tests.test_instagram_test_mode import Profiles
from tests.test_instagram_worker import CUSTOMER, DISCLOSURE, OTHER_CUSTOMER, PretendSender, Setup

PROFILES = {CUSTOMER: ("sara.k", "Sara"), OTHER_CUSTOMER: ("ahmad.q", None)}


@pytest.fixture
def s(tmp_path):
    setup = Setup(tmp_path / "cozysetup.db", client=PretendOpenAI(
        answer(text("Hello! How can I help?")),
        answer(call("show_booking_summary", **BOOKING)), answer(text("Please check your booking details...")),
        answer(call("create_booking", call_id="call_2", **BOOKING, customer_language="en")),
        answer(text("Your booking CS-0001 has been created.")),
    ), profiles=Profiles(PROFILES))
    yield setup
    setup.close()


def run(s, *argv, reply="y"):
    return admin.main(["--db", str(s.path), *argv], clock=s.clock, ask=lambda question: reply)


def chat(s, *messages, customer=CUSTOMER):
    s.receive(*(s.dm(words, customer=customer) for words in messages))
    s.worker.run_once()


def booked(s):
    chat(s, "Hi")
    chat(s, "Julaia, 1 October, deposit, Ahmad, 99999999")
    chat(s, "yes")
    return s.service.get_booking("CS-0001")


# --- conversations / conversation ---------------------------------------------------------------------

def test_conversations_lists_every_chat_by_username(s, capsys):
    chat(s, "Hi")
    s.receive(s.dm("Owner here", customer=OTHER_CUSTOMER, echo=True))
    s.worker.run_once()
    assert run(s, "conversations") == 0
    out = capsys.readouterr().out
    assert "@sara.k (Sara)" in out and "AI answering · language ?" in out
    assert "@ahmad.q" in out and "⏸ AI paused (The owner replied in Instagram)" in out


def test_conversations_when_there_are_none(s, capsys):
    assert run(s, "conversations") == 0
    assert "No Instagram conversations yet." in capsys.readouterr().out


def test_conversation_shows_the_transcript_bookings_and_state(s, capsys):
    booked(s)
    assert run(s, "conversation", "@Sara.K") == 0
    out = capsys.readouterr().out
    assert "@sara.k (Sara)" in out and "AI answering" in out
    assert "Instagram's 24-hour window is open" in out
    assert "Booking CS-0001: Thu 1 Oct 2026, Julaia - waiting for payment" in out
    assert f"AI:       {DISCLOSURE}" in out
    assert "Customer: Hi" in out
    assert "AI:       Hello! How can I help?" in out
    assert "(AI used show_booking_summary)" in out and "(AI used create_booking)" in out
    assert "Customer: yes" in out


def test_conversation_by_instagram_id_and_unknown(s, capsys):
    chat(s, "Hi")
    assert run(s, "conversation", CUSTOMER) == 0
    assert run(s, "conversation", "@nobody") == 1
    assert "No Instagram conversation with @nobody" in capsys.readouterr().err


def test_conversation_shows_the_owners_own_replies_and_unsent_replies(tmp_path, capsys):
    s = Setup(tmp_path / "x.db", client=PretendOpenAI(answer(text("Hello!"))),
              sender=PretendSender(SendError("busy")), profiles=Profiles(PROFILES))
    try:
        chat(s, "Hi")
        s.receive(s.dm("I'll call you", echo=True))
        s.worker.run_once()
        assert run(s, "conversation", "@sara.k") == 0
        out = capsys.readouterr().out
        assert "You:      I'll call you" in out
        assert "⏸ AI paused since" in out and "resume @sara.k" in out
        assert "Replies not sent:" in out and "cancelled - the owner took over the conversation" in out
    finally:
        s.close()


def test_readable_history_hides_the_ais_internal_steps():
    history = [
        {"role": "assistant", "content": DISCLOSURE},
        {"role": "user", "content": "Hi"},
        {"type": "reasoning", "id": "rs_1", "encrypted_content": "..."},
        {"type": "function_call", "name": "check_availability", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "{...}"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "It's free!"}]},
        {"role": "assistant", "content": "(The owner replied personally: See you)"},
    ]
    assert readable_history(history) == [("ai", DISCLOSURE), ("customer", "Hi"), ("tool", "check_availability"),
                                         ("ai", "It's free!"), ("owner", "See you")]


# --- pause / resume ------------------------------------------------------------------------------------------

def test_pause_then_resume(s, capsys):
    chat(s, "Hi")
    assert run(s, "pause", "@sara.k", "I'll handle the discount") == 0
    stored = s.conversation()
    assert stored.ai_paused and stored.pause_reason == "I'll handle the discount"
    chat(s, "Can I get a discount?")
    assert len(s.client.requests) == 1                              # the AI didn't answer
    assert run(s, "resume", "sara.k") == 0
    assert not s.conversation().ai_paused
    assert "The AI answers @sara.k (Sara) again" in capsys.readouterr().out


def test_pause_asks_first_and_no_changes_nothing(s, capsys):
    chat(s, "Hi")
    assert run(s, "pause", "@sara.k", reply="n") == 1
    assert not s.conversation().ai_paused
    assert "Confirmations and reminders for their bookings are still sent." in capsys.readouterr().out


def test_pause_and_resume_when_already_so_or_unknown(s, capsys):
    chat(s, "Hi")
    assert run(s, "resume", "@sara.k") == 0
    assert "already answering" in capsys.readouterr().out
    run(s, "pause", "@sara.k", "-y")
    assert run(s, "pause", "@sara.k") == 0
    assert "already paused" in capsys.readouterr().out
    assert run(s, "pause", "@nobody") == 1


def test_pausing_stops_replies_that_were_not_sent_yet(tmp_path):
    s = Setup(tmp_path / "x.db", client=PretendOpenAI(answer(text("Hello!"))),
              sender=PretendSender(SendError("busy")), profiles=Profiles(PROFILES))
    try:
        chat(s, "Hi")
        run(s, "pause", "@sara.k")
        s.clock.advance(minutes=2)
        s.worker.run_once()
        assert s.sender.sent == []
        assert {r["status"] for r in s.replies()} == {"cancelled"}
    finally:
        s.close()


# --- Instagram customers everywhere the owner looks ------------------------------------------------------

def test_handoffs_say_which_instagram_customer(s, capsys):
    chat(s, "Hi")
    s.service.create_handoff("weather", "Customer asks about the wind on Thursday",
                             channel="instagram", channel_user_id=CUSTOMER)
    assert run(s, "handoffs") == 0
    assert "Instagram: @sara.k (Sara)" in capsys.readouterr().out


def test_show_says_which_instagram_customer_made_the_booking(s, capsys):
    booked(s)
    assert run(s, "show", "CS-0001") == 0
    assert "Came via:    instagram  @sara.k (Sara)" in capsys.readouterr().out


def test_approving_an_instagram_booking_explains_the_24_hour_rule(s, capsys):
    booked(s)
    assert run(s, "approve", "CS-0001", "received", "-y") == 0
    out = capsys.readouterr().out
    assert "Confirmation queued for Instagram" in out and "last 24 hours" in out
    assert "goes out with cozysetup-outbox." not in out


def test_the_outbox_says_whom_to_write_to_on_instagram(s, capsys):
    booked(s)
    s.service.approve_payment("CS-0001")
    s.clock.advance(hours=25)                                        # the window has closed
    outbox.deliver_due(s.service, {"instagram": PretendInstagram()})
    assert run(s, "outbox") == 0
    assert "→ Instagram DM to @sara.k (Sara) Ahmad (+96599999999)" in capsys.readouterr().out


# --- overview ------------------------------------------------------------------------------------------------

def test_overview_shows_how_instagram_is_doing(s, capsys):
    chat(s, "Hi")
    s.receive(s.dm("Owner here", customer=OTHER_CUSTOMER, echo=True))
    s.worker.run_once()
    assert run(s, "overview") == 0
    out = capsys.readouterr().out
    assert "\nINSTAGRAM\n" in out
    assert "Worker running - last round" in out
    assert "⏸ AI paused for @ahmad.q" in out


def test_overview_flags_a_stopped_worker_and_unanswered_people(tmp_path, capsys):
    s = Setup(tmp_path / "x.db", client=PretendOpenAI(), profiles=Profiles(PROFILES), only={"sara.k"})
    try:
        s.receive(s.dm("Hi", customer=OTHER_CUSTOMER))
        s.worker.run_once()
        s.clock.advance(minutes=10)
        assert run(s, "overview") == 0
        out = capsys.readouterr().out
        assert "Instagram needs you - see below" in out
        assert "is it still running?" in out
        assert "1 message(s) from people not on the test list" in out
    finally:
        s.close()


def test_overview_without_instagram_has_no_instagram_section(s, capsys):
    assert run(s, "overview") == 0
    assert "INSTAGRAM" not in capsys.readouterr().out
