"""Tests for the agent loop, with a pretend OpenAI model - no API calls, no cost."""

import json
from datetime import datetime
from types import SimpleNamespace

import httpx2
import openai
import pytest

from cozysetup.agent import MAX_TOOL_ROUNDS, Agent, ConversationLog, openai_tool
from cozysetup.bookings import BookingService
from cozysetup.business_info import load_business_info
from cozysetup.database import BookingStatus, HandoffType, connect
from cozysetup.tools import Conversation, tool_definitions

INFO = load_business_info()
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=INFO.timezone)   # Monday 2 PM, Kuwait
HANDOFF_REPLY = "I've passed your request to the owner, who will get back to you."


# --- A pretend OpenAI model (Responses API shapes) ----------------------------------------

def text(words):
    return SimpleNamespace(type="message", role="assistant",
                           content=[SimpleNamespace(type="output_text", text=words)])


def refusal(words="I can't help with that."):
    return SimpleNamespace(type="message", role="assistant",
                           content=[SimpleNamespace(type="refusal", refusal=words)])


def reasoning():
    return SimpleNamespace(type="reasoning", id="rs_1", summary=[], encrypted_content="<encrypted>")


def call(name, call_id="call_1", **arguments):
    return SimpleNamespace(type="function_call", call_id=call_id, name=name, arguments=json.dumps(arguments))


def answer(*items, status="completed", incomplete_reason=None):
    usage = SimpleNamespace(input_tokens=2100, output_tokens=20,
                            input_tokens_details=SimpleNamespace(cached_tokens=2000, cache_write_tokens=0))
    incomplete = SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None
    return SimpleNamespace(output=list(items), status=status, incomplete_details=incomplete,
                           usage=usage, model="gpt-6-sol")


class PretendOpenAI:
    """Gives scripted answers in order, and remembers every request it received."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.responses = self   # so the agent can call client.responses.create(...)

    def create(self, **request):
        # Copy the input: the agent keeps adding to the same list afterwards.
        self.requests.append({**request, "input": list(request["input"])})
        next_answer = self.answers.pop(0)
        if isinstance(next_answer, Exception):
            raise next_answer
        return next_answer


@pytest.fixture
def service(tmp_path):
    db = connect(":memory:")
    yield BookingService(db, INFO, clock=lambda: NOW, proofs_dir=tmp_path / "proofs")
    db.close()


def make_agent(service, model, log=None):
    return Agent(model, service, Conversation("terminal", "customer-1"), log=log)


BOOKING = {"date": "2026-10-01", "location_id": "julaia", "customer_name": "Ahmad",
           "customer_phone": "99999999", "payment_choice": "deposit"}


def outputs_sent(request):
    return [item for item in request["input"] if isinstance(item, dict) and item.get("type") == "function_call_output"]


# --- The request sent to the model --------------------------------------------------------

def test_request_uses_the_chosen_model_stateless_low_effort_and_todays_date(service):
    model = PretendOpenAI(answer(text("Hi! How can I help?")))
    make_agent(service, model).reply("hello")
    request = model.requests[0]
    assert request["model"] == "gpt-6-sol"
    assert request["store"] is False
    assert request["reasoning"] == {"effort": "low"}
    assert request["instructions"].startswith("You are the customer service assistant of CozySetup.kw")
    assert request["instructions"].endswith("Today is Monday 28 September 2026 (2026-09-28) in Kuwait.")
    assert request["input"] == [{"role": "user", "content": "hello"}]


def test_instructions_before_the_date_are_identical_every_day_so_they_are_cached(service):
    model = PretendOpenAI(answer(text("Hi")), answer(text("Hi")))
    make_agent(service, model).reply("hello")
    later = BookingService(service.db, INFO, clock=lambda: datetime(2026, 10, 5, 9, 0, tzinfo=INFO.timezone))
    make_agent(later, model).reply("hello")
    first, second = (r["instructions"].rsplit("\n\nToday is", 1)[0] for r in model.requests)
    assert first == second


def test_tools_are_strict_openai_functions():
    tools = [openai_tool(definition) for definition in tool_definitions(INFO)]
    assert [t["name"] for t in tools] == [
        "check_availability", "show_booking_summary", "create_booking", "attach_payment_proof",
        "get_booking_status", "handoff_to_human",
    ]
    for tool in tools:
        assert tool["type"] == "function" and tool["strict"] is True
        assert tool["parameters"]["additionalProperties"] is False
        assert tool["parameters"]["required"] == list(tool["parameters"]["properties"])


def test_optional_handoff_fields_use_openais_null_union():
    handoff = openai_tool(tool_definitions(INFO)[5])["parameters"]["properties"]
    assert handoff["customer_phone"]["type"] == ["string", "null"]
    assert "anyOf" not in handoff["customer_phone"]
    # tools.py itself is unchanged
    assert "anyOf" in tool_definitions(INFO)[5]["input_schema"]["properties"]["customer_phone"]


def test_plain_answer_goes_to_the_customer(service):
    reply = make_agent(service, PretendOpenAI(answer(reasoning(), text("Hi! How can I help?")))).reply("hello")
    assert reply.text == "Hi! How can I help?"
    assert reply.tools_used == []
    assert not reply.handed_off_after_problem


def test_the_conversation_is_remembered_between_messages(service):
    model = PretendOpenAI(answer(text("Hi!")), answer(text("Sure.")))
    agent = make_agent(service, model)
    agent.reply("hello")
    agent.reply("thanks")
    second_input = model.requests[1]["input"]
    assert second_input[0] == {"role": "user", "content": "hello"}
    assert second_input[1].type == "message"
    assert second_input[2] == {"role": "user", "content": "thanks"}


# --- Tools ----------------------------------------------------------------------------------

def test_tool_is_run_and_its_result_sent_back(service):
    model = PretendOpenAI(
        answer(call("check_availability", "call_1", date="2026-10-01")),
        answer(text("Thursday is available!")),
    )
    reply = make_agent(service, model).reply("Is Thursday free?")
    assert reply.text == "Thursday is available!"
    assert reply.tools_used == ["check_availability"]

    [output] = outputs_sent(model.requests[1])
    assert output["call_id"] == "call_1"
    assert json.loads(output["output"])["status"] == "available"


def test_reasoning_items_are_sent_back_with_the_tool_results(service):
    thinking = reasoning()
    model = PretendOpenAI(
        answer(thinking, call("check_availability", date="2026-10-01")),
        answer(text("Free!")),
    )
    make_agent(service, model).reply("Thursday?")
    assert thinking in model.requests[1]["input"]


def test_several_tools_in_one_answer_send_all_results_together(service):
    model = PretendOpenAI(
        answer(call("check_availability", "a", date="2026-10-01"), call("check_availability", "b", date="2026-10-02")),
        answer(text("Both are free.")),
    )
    make_agent(service, model).reply("Thursday or Friday?")
    assert [o["call_id"] for o in outputs_sent(model.requests[1])] == ["a", "b"]


def test_a_refused_tool_is_reported_to_the_model(service):
    model = PretendOpenAI(
        answer(call("show_booking_summary", **{**BOOKING, "customer_phone": "123"})),
        answer(text("Could you send your phone number again?")),
    )
    make_agent(service, model).reply("My number is 123")
    [output] = outputs_sent(model.requests[1])
    assert json.loads(output["output"])["refused"] == "invalid_phone"


def test_arguments_that_are_not_json_are_reported_not_crashed_on(service):
    broken = SimpleNamespace(type="function_call", call_id="c", name="check_availability", arguments="{oops")
    model = PretendOpenAI(answer(broken), answer(text("Sorry, which date?")))
    assert make_agent(service, model).reply("Thursday?").text == "Sorry, which date?"
    assert "not valid JSON" in outputs_sent(model.requests[1])[0]["output"]


def test_a_whole_booking_across_several_messages(service):
    model = PretendOpenAI(
        answer(call("show_booking_summary", **BOOKING)), answer(text("<summary>")),
        answer(call("create_booking", **BOOKING)), answer(text("<payment instructions>")),
    )
    agent = make_agent(service, model)
    agent.reply("Ahmad, 99999999, deposit")
    assert service.list_bookings() == []                      # summary only: nothing saved
    agent.reply("Yes, correct")
    booking = service.get_booking("CS-0001")
    assert booking.status is BookingStatus.PENDING_PAYMENT     # never confirmed by the AI
    assert booking.channel_user_id == "customer-1"


def test_text_said_before_a_tool_is_kept(service):
    model = PretendOpenAI(
        answer(text("Let me check."), call("check_availability", date="2026-10-01")),
        answer(text("Yes, it's free!")),
    )
    assert make_agent(service, model).reply("Thursday?").text == "Let me check.\n\nYes, it's free!"


# --- Images ------------------------------------------------------------------------------------

def test_images_are_numbered_for_the_model_but_never_sent_to_it(service):
    model = PretendOpenAI(answer(text("Thanks!")))
    make_agent(service, model).reply("Here is my transfer", images=[b"\xff\xd8\xff" + bytes(50)])
    assert model.requests[0]["input"][0]["content"] == "Here is my transfer\n[Customer attached image #1]"


def test_screenshot_reaches_the_booking(service):
    model = PretendOpenAI(
        answer(call("show_booking_summary", **BOOKING)), answer(text("<summary>")),
        answer(call("create_booking", **BOOKING)), answer(text("<pay>")),
        answer(call("attach_payment_proof", reference="CS-0001", customer_phone="99999999", attachment_number=1)),
        answer(text("Thank you, we've received your screenshot.")),
    )
    agent = make_agent(service, model)
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
    error = openai.APIConnectionError(request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))
    reply = make_agent(service, PretendOpenAI(error)).reply("hello")
    handoff = assert_handed_over(service, reply)
    assert "APIConnectionError" in handoff.summary and "'hello'" in handoff.summary


def test_model_refuses(service):
    reply = make_agent(service, PretendOpenAI(answer(refusal()))).reply("something odd")
    assert "refused" in assert_handed_over(service, reply).summary


def test_answer_cut_off(service):
    reply = make_agent(service, PretendOpenAI(
        answer(status="incomplete", incomplete_reason="max_output_tokens"))).reply("hi")
    assert "incomplete (max_output_tokens)" in assert_handed_over(service, reply).summary


def test_empty_answer(service):
    reply = make_agent(service, PretendOpenAI(answer(reasoning()))).reply("hi")
    assert "empty answer" in assert_handed_over(service, reply).summary


def test_too_many_tool_rounds(service):
    endless = [answer(call("check_availability", date="2026-10-01")) for _ in range(MAX_TOOL_ROUNDS + 1)]
    model = PretendOpenAI(*endless)
    reply = make_agent(service, model).reply("hi")
    assert len(model.requests) == MAX_TOOL_ROUNDS + 1          # it stopped
    assert "tool rounds" in assert_handed_over(service, reply).summary


def test_the_conversation_can_continue_after_a_problem(service):
    model = PretendOpenAI(answer(refusal()), answer(text("How can I help?")))
    agent = make_agent(service, model)
    agent.reply("something odd")
    assert agent.reply("hello again").text == "How can I help?"
    assert model.requests[1]["input"] == [
        {"role": "user", "content": "something odd"},
        {"role": "assistant", "content": HANDOFF_REPLY},
        {"role": "user", "content": "hello again"},
    ]


# --- Cost and log ----------------------------------------------------------------------------------

def test_usage_is_added_up_across_the_rounds(service):
    model = PretendOpenAI(answer(call("check_availability", date="2026-10-01")), answer(text("Free!")))
    usage = make_agent(service, model).reply("Thursday?").usage
    # Each pretend response: 2100 input of which 2000 cached, 20 output.
    assert (usage.requests, usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (2, 200, 4000, 40)
    assert usage.cost_usd("gpt-6-sol") == pytest.approx((200 * 2.00 + 4000 * 0.20 + 40 * 10.00) / 1_000_000)


def test_everything_is_logged(service, tmp_path):
    log = ConversationLog(tmp_path / "conversations" / "test.jsonl")
    model = PretendOpenAI(answer(call("check_availability", date="2026-10-01")), answer(text("Free!")))
    make_agent(service, model, log=log).reply("Thursday?")
    events = [json.loads(line) for line in log.path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["customer", "ai", "tool", "ai", "reply"]
    assert events[2]["name"] == "check_availability"
    assert events[2]["input"] == {"date": "2026-10-01"}
    assert events[-1]["text"] == "Free!"
    assert "cost_usd" in events[-1]
