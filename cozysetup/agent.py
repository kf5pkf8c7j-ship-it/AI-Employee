"""The agent loop: the AI employee's conversation with one customer.

Uses OpenAI's Responses API. For each customer message:
  1. send the model the instructions, the conversation so far and the tools
  2. if the model asks to use tools (function calls), run them through the
     BookingService and send the results back - then go to 1
  3. when the model answers, that answer goes to the customer

If anything goes wrong (the API is unreachable, the model refuses, the
answer is cut off, too many steps), the case is handed to the owner and the
customer gets the owner's handoff reply - never an error message and never
silence.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import openai

from cozysetup.bookings import BookingService
from cozysetup.database import DEFAULT_DB_PATH, HandoffType, Language
from cozysetup.language import detect_language, has_arabic_script
from cozysetup.prompt import build_system_prompt, today_context
from cozysetup.replies import render
from cozysetup.settings import MODEL, estimated_cost_usd
from cozysetup.tools import Conversation, ToolResult, Tools, tool_definitions

MAX_OUTPUT_TOKENS = 16_000

# The Arabizi guard: instructions for the one-off rewrite request.
ARABIZI_REWRITE_INSTRUCTIONS = """\
Rewrite the message below as Arabizi: Kuwaiti Arabic written only in Latin letters and numbers \
(e.g. 3 for ع, 7 for ح). No Arabic script at all - not a single Arabic letter.
Keep everything else exactly the same: every price, amount, "dinar", date, time, booking \
reference (like CS-0001), phone number, name, the Wamd and payment wording, anything in square \
brackets, and the line breaks. Do not add, remove or change any information.
Reply with the rewritten message only."""
# Numbers that are facts (prices, dates, times, CS-0001, phone numbers) - not the digits
# used as letters inside Arabizi words like "7awwel" or "3abr", whose spelling may vary.
_NUMBER = re.compile(r"(?<![A-Za-z])\d+(?![A-Za-z])")
EFFORT = "low"             # customer chat; raise in Step 5 if the tests show it's needed
MAX_TOOL_ROUNDS = 10       # per customer message - stops a confused loop from running up costs
DEFAULT_LOG_DIR = DEFAULT_DB_PATH.parent / "conversations"


@dataclass
class Usage:
    input_tokens: int = 0         # uncached input
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0

    def add(self, usage) -> None:
        """Add one response's usage. OpenAI's input_tokens is the total; cached
        reads and cache writes are parts of it, billed at their own rates."""
        details = getattr(usage, "input_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        written = getattr(details, "cache_write_tokens", 0) or 0
        self.input_tokens += usage.input_tokens - cached - written
        self.cache_read_tokens += cached
        self.cache_write_tokens += written
        self.output_tokens += usage.output_tokens
        self.requests += 1

    def cost_usd(self, model: str) -> float:
        return estimated_cost_usd(model, self.input_tokens, self.output_tokens,
                                  self.cache_write_tokens, self.cache_read_tokens)


@dataclass
class AgentReply:
    text: str                                      # what the customer sees
    tools_used: list[str] = field(default_factory=list)
    handed_off_after_problem: bool = False         # True if something went wrong
    usage: Usage = field(default_factory=Usage)


class ConversationLog:
    """One JSON line per event, so the owner (and we) can see exactly what happened."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **details) -> None:
        line = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **details}
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")


def openai_tool(definition: dict) -> dict:
    """One of our tool definitions in the Responses API's function-tool format.

    tools.py stays provider-neutral; only the wrapping differs here.
    """
    return {
        "type": "function",
        "name": definition["name"],
        "description": definition["description"],
        "strict": definition["strict"],
        "parameters": _openai_schema(definition["input_schema"]),
    }


def _openai_schema(schema: dict) -> dict:
    """Optional fields: our anyOf [X, null] becomes OpenAI's documented "type": [X, "null"]."""
    schema = copy.deepcopy(schema)
    for prop in schema.get("properties", {}).values():
        options = prop.get("anyOf")
        if options and len(options) == 2 and {"type": "null"} in options:
            other = next(option for option in options if option != {"type": "null"})
            del prop["anyOf"]
            prop["type"] = [other["type"], "null"]
    return schema


class Agent:
    def __init__(
        self,
        client: openai.OpenAI,
        service: BookingService,
        conversation: Conversation,
        *,
        model: str = MODEL,
        effort: str = EFFORT,
        log: ConversationLog | None = None,
    ):
        self.client = client
        self.service = service
        self.info = service.info
        self.conversation = conversation
        self.tools = Tools(service, conversation)
        self.model = model
        self.effort = effort
        # The customer's language as far as we can tell (see language.py); drives the Arabizi guard.
        self.customer_language: Language | None = None
        self.log = log
        # The whole conversation, replayed on every request. With store=False
        # nothing is kept on OpenAI's side, so this list is the only history.
        self.messages: list = []
        # Built once and identical on every request, so OpenAI caches them automatically.
        self._system_prompt = build_system_prompt(self.info)
        self._tools = [openai_tool(definition) for definition in tool_definitions(self.info)]

    def reply(self, text: str, images: list[bytes] = ()) -> AgentReply:
        """Handle one customer message and return the answer for the customer."""
        content = text.strip()
        for image in images:
            number = self.conversation.add_attachment(image)
            # The model is told an image arrived, but never sees it - screenshots are not judged.
            content += f"\n[Customer attached image #{number}]"
        self.messages.append({"role": "user", "content": content.strip()})
        self.customer_language = detect_language(text) or self.customer_language
        self._log("customer", text=content.strip())

        result = AgentReply(text="")
        said: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS + 1):
            try:
                response = self.client.responses.create(**self._request())
            except openai.APIError as error:
                # The SDK already retried; the service is really unavailable.
                return self._hand_over(result, f"OpenAI API error: {type(error).__name__}: {error}")
            result.usage.add(response.usage)
            self._log("ai", status=response.status,
                      output=[_plain(item) for item in response.output], usage=_plain(response.usage))

            if response.status != "completed":
                reason = getattr(response.incomplete_details, "reason", None)
                return self._hand_over(result, f"The response was {response.status} ({reason}).")
            if _refused(response):
                return self._hand_over(result, "The model refused to answer this message.")

            # Keep every output item (reasoning included) - they must be replayed with tool results.
            self.messages.extend(response.output)
            said += [text for text in _texts(response) if text]
            calls = [item for item in response.output if item.type == "function_call"]

            if not calls:
                if not said:
                    return self._hand_over(result, "The model gave an empty answer.")
                result.text = "\n\n".join(said)
                if self.customer_language is Language.ARABIZI and has_arabic_script(result.text):
                    result.text = self._arabizi_guard(result.text, result)
                self._log("reply", text=result.text, cost_usd=round(result.usage.cost_usd(self.model), 6))
                return result

            # Run every tool the model asked for, and send all results back together.
            for call in calls:
                outcome = self._run_tool(call)
                result.tools_used.append(call.name)
                self.messages.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": outcome.content,
                })

        return self._hand_over(result, f"More than {MAX_TOOL_ROUNDS} tool rounds for one message.")

    # --- Helpers ------------------------------------------------------------------------

    def _request(self) -> dict:
        return {
            "model": self.model,
            # The long, unchanging instructions first; the date (changes once a day) last.
            "instructions": f"{self._system_prompt}\n\n{today_context(self.service.today())}",
            "input": self.messages,
            "tools": self._tools,
            "reasoning": {"effort": self.effort},
            "store": False,   # stateless: customer details stay in our database and logs only
            "max_output_tokens": MAX_OUTPUT_TOKENS,
        }

    def _arabizi_guard(self, text: str, result: AgentReply) -> str:
        """An Arabizi customer must never get Arabic letters. Ask the model once to
        rewrite the reply in Latin letters; use the rewrite only if it has no Arabic
        letters left and still contains every number of the original (prices, dates,
        times, references, phones). Otherwise send the original."""
        try:
            response = self.client.responses.create(
                model=self.model,
                instructions=ARABIZI_REWRITE_INSTRUCTIONS,
                input=[{"role": "user", "content": text}],
                reasoning={"effort": self.effort},
                store=False,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            )
        except openai.APIError as error:
            self._log("arabizi_guard", outcome="kept original", reason=f"{type(error).__name__}: {error}",
                      original=text)
            return text
        result.usage.add(response.usage)
        rewritten = "\n\n".join(_texts(response)).strip()

        missing = sorted(set(_NUMBER.findall(text)) - set(_NUMBER.findall(rewritten)))
        if response.status != "completed" or _refused(response) or not rewritten:
            reason = "the rewrite request did not complete"
        elif has_arabic_script(rewritten):
            reason = "the rewrite still contains Arabic letters"
        elif missing:
            reason = f"the rewrite lost numbers: {missing}"
        else:
            self._log("arabizi_guard", outcome="rewritten", original=text, rewritten=rewritten)
            return rewritten
        self._log("arabizi_guard", outcome="kept original", reason=reason, original=text, rewritten=rewritten)
        return text

    def _run_tool(self, call) -> ToolResult:
        try:
            arguments = json.loads(call.arguments)
        except json.JSONDecodeError:
            outcome = ToolResult(json.dumps({"error": "The arguments were not valid JSON."}), is_error=True)
            arguments = call.arguments
        else:
            outcome = self.tools.run(call.name, arguments)
            if call.name == "create_booking" and not outcome.is_error:
                try:
                    self.customer_language = Language(arguments.get("customer_language"))
                except ValueError:
                    pass
        self._log("tool", name=call.name, input=arguments, result=outcome.content, is_error=outcome.is_error)
        return outcome

    def _hand_over(self, result: AgentReply, problem: str) -> AgentReply:
        """Something went wrong: the owner takes over, and the customer is told so politely."""
        last_customer_text = next(
            (m["content"] for m in reversed(self.messages)
             if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)),
            "",
        )
        self.service.create_handoff(
            HandoffType.OTHER,
            f"The AI could not handle this conversation ({problem}). "
            f"Customer's last message: {last_customer_text!r}",
            channel=self.conversation.channel,
            channel_user_id=self.conversation.channel_user_id,
        )
        result.text = render(self.info, "handoff")["en"]
        result.handed_off_after_problem = True
        # Keep the history meaningful for the next message.
        self.messages.append({"role": "assistant", "content": result.text})
        self._log("handed_over", problem=problem, reply=result.text)
        return result

    def _log(self, event: str, **details) -> None:
        if self.log:
            self.log.write(event, **details)


def _texts(response) -> list[str]:
    return [
        part.text.strip()
        for item in response.output if item.type == "message"
        for part in item.content if part.type == "output_text"
    ]


def _refused(response) -> bool:
    return any(
        part.type == "refusal"
        for item in response.output if item.type == "message"
        for part in item.content
    )


def _plain(value) -> object:
    """A response part as plain data, for the log."""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return {key: _plain(item) for key, item in vars(value).items()}
    return value
