"""Tests for the agent loop, with a pretend Claude - no API calls, no cost."""

import json
from datetime import datetime
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from cozysetup.agent import MAX_TOOL_ROUNDS, Agent, ConversationLog
from cozysetup.bookings import BookingService
from cozysetup.business_info import load_business_info
from cozysetup.database import BookingStatus, HandoffType, connect
from cozysetup.tools import Conversation

INFO = load_business_info()
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=INFO.timezone)   # Monday 2 PM, Kuwait
HANDOFF_REPLY = "I've passed your request to the owner, who will get back to you."


# --- A pretend Claude ------------------------------------------------------------------

def text(words):
    return SimpleNamespace(type="text", text=words)


def tool(name, tool_id="call_1", **tool_input):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def answer(*blocks, stop_reason=None):
    if stop_reason is None:
        stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                            cache_creation_input_tokens=0, cache_read_input_tokens=2000)
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, usage=usage, model="claude-sonnet-5")


class PretendClaude:
    """Gives scripted answers in order, and remembers every request it received."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.messages = self   # so the agent can call client.messages.create(...)

    def create(self, **request):
        # Copy the messages: the agent keeps adding to the same list afterwards.
        self.requests.append({**request, "messages": list(request["messages"])})
        next_answer = self.answers.pop(0)
        if isinstance(next_answer, Exception):
            raise next_answer
        return next_answer


@pytest.fixture
def service(tmp_path):
    db = connect(":memory:")
    yield BookingService(db, INFO, clock=lambda: NOW, proofs_dir=tmp_path / "proofs")
    db.close()


def make_agent(service, claude, log=None):
    return Agent(claude, service, Conversation("terminal", "customer-1"), log=log)


BOOKING = {"date": "2026-10-01", "location_id": "julaia", "customer_name": "Ahmad",
           "customer_phone": "99999999", "payment_choice": "deposit"}


# --- The request sent to Claude --------------------------------------------------------

def test_request_uses_the_chosen_model_cached_instructions_and_todays_date(service):
    claude = PretendClaude(answer(text("Hi! How can I help?")))
    make_agent(service, claude).reply("hello")
    request = claude.requests[0]
    assert request["model"] == "claude-sonnet-5"
    assert request["output_config"] == {"effort": "low"}
    cached, today = request["system"]
    assert cached["cache_control"] == {"type": "ephemeral"}
    assert "customer service assistant of CozySetup.kw" in cached["text"]
    assert today == {"type": "text", "text": "Today is Monday 28 September 2026 (2026-09-28) in Kuwait."}
    assert [t["name"] for t in request["tools"]][0] == "check_availability"
    assert request["messages"] == [{"role": "user", "content": "hello"}]


def test_plain_answer_goes_to_the_customer(service):
    reply = make_agent(service, PretendClaude(answer(text("Hi! How can I help?")))).reply("hello")
    assert reply.text == "Hi! How can I help?"
    assert reply.tools_used == []
    assert not reply.handed_off_after_problem


def test_the_conversation_is_remembered_between_messages(service):
    claude = PretendClaude(answer(text("Hi!")), answer(text("Sure.")))
    agent = make_agent(service, claude)
    agent.reply("hello")
    agent.reply("thanks")
    roles = [m["role"] for m in claude.requests[1]["messages"]]
    assert roles == ["user", "assistant", "user"]


# --- Tools ----------------------------------------------------------------------------------

def test_tool_is_run_and_its_result_sent_back(service):
    claude = PretendClaude(
        answer(tool("check_availability", "call_1", date="2026-10-01")),
        answer(text("Thursday is available!")),
    )
    reply = make_agent(service, claude).reply("Is Thursday free?")
    assert reply.text == "Thursday is available!"
    assert reply.tools_used == ["check_availability"]

    [tool_result] = claude.requests[1]["messages"][-1]["content"]
    assert tool_result["tool_use_id"] == "call_1"
    assert tool_result["is_error"] is False
    assert json.loads(tool_result["content"])["status"] == "available"


def test_several_tools_in_one_answer_send_all_results_in_one_message(service):
    claude = PretendClaude(
        answer(tool("check_availability", "a", date="2026-10-01"), tool("check_availability", "b", date="2026-10-02")),
        answer(text("Both are free.")),
    )
    make_agent(service, claude).reply("Thursday or Friday?")
    results = claude.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]


def test_a_refused_tool_is_reported_to_claude_as_an_error(service):
    claude = PretendClaude(
        answer(tool("show_booking_summary", **{**BOOKING, "customer_phone": "123"})),
        answer(text("Could you send your phone number again?")),
    )
    make_agent(service, claude).reply("My number is 123")
    [tool_result] = claude.requests[1]["messages"][-1]["content"]
    assert tool_result["is_error"] is True
    assert json.loads(tool_result["content"])["refused"] == "invalid_phone"


def test_a_whole_booking_across_several_messages(service):
    claude = PretendClaude(
        answer(tool("show_booking_summary", **BOOKING)), answer(text("<summary>")),
        answer(tool("create_booking", **BOOKING)), answer(text("<payment instructions>")),
    )
    agent = make_agent(service, claude)
    agent.reply("Ahmad, 99999999, deposit")
    assert service.list_bookings() == []                      # summary only: nothing saved
    agent.reply("Yes, correct")
    booking = service.get_booking("CS-0001")
    assert booking.status is BookingStatus.PENDING_PAYMENT     # never confirmed by the AI
    assert booking.channel_user_id == "customer-1"


def test_text_said_before_a_tool_is_kept(service):
    claude = PretendClaude(
        answer(text("Let me check."), tool("check_availability", date="2026-10-01")),
        answer(text("Yes, it's free!")),
    )
    assert make_agent(service, claude).reply("Thursday?").text == "Let me check.\n\nYes, it's free!"


# --- Images ------------------------------------------------------------------------------------

def test_images_are_numbered_for_claude_but_never_sent_to_it(service):
    claude = PretendClaude(answer(text("Thanks!")))
    make_agent(service, claude).reply("Here is my transfer", images=[b"\xff\xd8\xff" + bytes(50)])
    message = claude.requests[0]["messages"][0]
    assert message["content"] == "Here is my transfer\n[Customer attached image #1]"


def test_screenshot_reaches_the_booking(service):
    claude = PretendClaude(
        answer(tool("show_booking_summary", **BOOKING)), answer(text("<summary>")),
        answer(tool("create_booking", **BOOKING)), answer(text("<pay>")),
        answer(tool("attach_payment_proof", reference="CS-0001", customer_phone="99999999", attachment_number=1)),
        answer(text("Thank you, we've received your screenshot.")),
    )
    agent = make_agent(service, claude)
    agent.reply("details")
    agent.reply("yes")
    agent.reply("", images=[b"\xff\xd8\xff" + bytes(50)])
    assert service.get_booking("CS-0001").status is BookingStatus.PAYMENT_SUBMITTED


# --- When something goes wrong, the owner takes over ---------------------------------------------

def assert_handed_over(service, reply):
    assert reply.text == HANDOFF_REPLY
    assert reply.handed_off_after_problem
    [handoff] = service.list_handoffs()
    assert handoff.type is HandoffType.OTHER
    assert handoff.channel_user_id == "customer-1"
    return handoff


def test_api_unreachable(service):
    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    reply = make_agent(service, PretendClaude(error)).reply("hello")
    handoff = assert_handed_over(service, reply)
    assert "APIConnectionError" in handoff.summary and "'hello'" in handoff.summary


def test_claude_declines(service):
    reply = make_agent(service, PretendClaude(answer(stop_reason="refusal"))).reply("something odd")
    assert "declined" in assert_handed_over(service, reply).summary


def test_empty_answer(service):
    reply = make_agent(service, PretendClaude(answer(stop_reason="end_turn"))).reply("hi")
    assert "empty answer" in assert_handed_over(service, reply).summary


def test_too_many_tool_rounds(service):
    endless = [answer(tool("check_availability", date="2026-10-01")) for _ in range(MAX_TOOL_ROUNDS + 1)]
    claude = PretendClaude(*endless)
    reply = make_agent(service, claude).reply("hi")
    assert len(claude.requests) == MAX_TOOL_ROUNDS + 1          # it stopped
    assert "tool rounds" in assert_handed_over(service, reply).summary


def test_the_conversation_can_continue_after_a_problem(service):
    claude = PretendClaude(answer(stop_reason="refusal"), answer(text("How can I help?")))
    agent = make_agent(service, claude)
    agent.reply("something odd")
    assert agent.reply("hello again").text == "How can I help?"
    roles = [m["role"] for m in claude.requests[1]["messages"]]
    assert roles == ["user", "assistant", "user"]               # still a valid conversation


# --- Cost and log ----------------------------------------------------------------------------------

def test_usage_is_added_up_across_the_rounds(service):
    claude = PretendClaude(answer(tool("check_availability", date="2026-10-01")), answer(text("Free!")))
    usage = make_agent(service, claude).reply("Thursday?").usage
    assert (usage.requests, usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (2, 200, 40, 4000)
    assert usage.cost_usd("claude-sonnet-5") == pytest.approx((200 * 2 + 4000 * 0.2 + 40 * 10) / 1_000_000)


def test_everything_is_logged(service, tmp_path):
    log = ConversationLog(tmp_path / "conversations" / "test.jsonl")
    claude = PretendClaude(answer(tool("check_availability", date="2026-10-01")), answer(text("Free!")))
    make_agent(service, claude, log=log).reply("Thursday?")
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["customer", "claude", "tool", "claude", "reply"]
    assert events[2]["name"] == "check_availability"
    assert events[-1]["text"] == "Free!"
    assert "cost_usd" in events[-1]
