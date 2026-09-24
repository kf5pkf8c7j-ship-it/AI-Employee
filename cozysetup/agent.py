"""The agent loop: the AI employee's conversation with one customer.

For each customer message:
  1. send Claude the instructions, the conversation so far and the tools
  2. if Claude asks to use tools, run them (through the BookingService) and
     send the results back - then go to 1
  3. when Claude answers, that answer goes to the customer

If anything goes wrong (the API is unreachable, Claude declines, too many
steps), the case is handed to the owner and the customer gets the owner's
handoff reply - never an error message and never silence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import anthropic

from cozysetup.bookings import BookingService
from cozysetup.database import DEFAULT_DB_PATH, HandoffType
from cozysetup.prompt import build_system_prompt, today_context
from cozysetup.replies import render
from cozysetup.settings import MODEL, estimated_cost_usd
from cozysetup.tools import Conversation, Tools, tool_definitions

MAX_TOKENS = 16_000
EFFORT = "low"             # customer chat; raise in Step 5 if the tests show it's needed
MAX_TOOL_ROUNDS = 10       # per customer message - stops a confused loop from running up costs
DEFAULT_LOG_DIR = DEFAULT_DB_PATH.parent / "conversations"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0

    def add(self, usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
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


class Agent:
    def __init__(
        self,
        client: anthropic.Anthropic,
        service: BookingService,
        conversation: Conversation,
        *,
        model: str = MODEL,
        log: ConversationLog | None = None,
    ):
        self.client = client
        self.service = service
        self.info = service.info
        self.conversation = conversation
        self.tools = Tools(service, conversation)
        self.model = model
        self.log = log
        self.messages: list[dict] = []   # the whole conversation, sent every time (the API has no memory)
        # Built once: identical on every request, so it is cached.
        self._system_prompt = build_system_prompt(self.info)
        self._tool_definitions = tool_definitions(self.info)

    def reply(self, text: str, images: list[bytes] = ()) -> AgentReply:
        """Handle one customer message and return the answer for the customer."""
        content = text.strip()
        for image in images:
            number = self.conversation.add_attachment(image)
            # Claude is told an image arrived, but never sees it - screenshots are not judged.
            content += f"\n[Customer attached image #{number}]"
        self.messages.append({"role": "user", "content": content.strip()})
        self._log("customer", text=content.strip())

        result = AgentReply(text="")
        said: list[str] = []
        for _ in range(MAX_TOOL_ROUNDS + 1):
            try:
                response = self.client.messages.create(**self._request())
            except anthropic.APIError as error:
                # The SDK already retried; the service is really unavailable.
                return self._hand_over(result, f"Claude API error: {type(error).__name__}: {error}")
            result.usage.add(response.usage)
            self._log("claude", stop_reason=response.stop_reason,
                      content=[_block_dict(block) for block in response.content],
                      usage=_block_dict(response.usage))

            if response.stop_reason == "refusal":
                return self._hand_over(result, "Claude declined to answer this message.")
            if response.stop_reason not in ("tool_use", "end_turn", "stop_sequence"):
                return self._hand_over(result, f"Claude stopped unexpectedly ({response.stop_reason}).")

            # Keep the full answer (including any thinking) in the history, as the API expects.
            self.messages.append({"role": "assistant", "content": response.content})
            said += [block.text.strip() for block in response.content if block.type == "text" and block.text.strip()]

            if response.stop_reason != "tool_use":
                if not said:
                    return self._hand_over(result, "Claude gave an empty answer.")
                result.text = "\n\n".join(said)
                self._log("reply", text=result.text, cost_usd=round(result.usage.cost_usd(self.model), 6))
                return result

            # Run every tool Claude asked for, and send all results back in one message.
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                outcome = self.tools.run(block.name, block.input)
                result.tools_used.append(block.name)
                self._log("tool", name=block.name, input=block.input,
                          result=outcome.content, is_error=outcome.is_error)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": outcome.content,
                    "is_error": outcome.is_error,
                })
            self.messages.append({"role": "user", "content": tool_results})

        return self._hand_over(result, f"More than {MAX_TOOL_ROUNDS} tool rounds for one message.")

    # --- Helpers ------------------------------------------------------------------------

    def _request(self) -> dict:
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "output_config": {"effort": EFFORT},
            "system": [
                # The long, unchanging instructions - cached.
                {"type": "text", "text": self._system_prompt, "cache_control": {"type": "ephemeral"}},
                # The only part that changes (once a day).
                {"type": "text", "text": today_context(self.service.today())},
            ],
            "tools": self._tool_definitions,
            "messages": self.messages,
            # Also cache the conversation so far, so each new message only pays for what's new.
            "cache_control": {"type": "ephemeral"},
        }

    def _hand_over(self, result: AgentReply, problem: str) -> AgentReply:
        """Something went wrong: the owner takes over, and the customer is told so politely."""
        last_customer_text = next(
            (m["content"] for m in reversed(self.messages) if m["role"] == "user" and isinstance(m["content"], str)),
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
        # Keep the history valid for the next message: it must end with an assistant turn.
        self.messages.append({"role": "assistant", "content": result.text})
        self._log("handed_over", problem=problem, reply=result.text)
        return result

    def _log(self, event: str, **details) -> None:
        if self.log:
            self.log.write(event, **details)


def _block_dict(block) -> object:
    """A response part as plain data, for the log."""
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    if hasattr(block, "__dict__"):
        return {key: _block_dict(value) for key, value in vars(block).items()}
    return block
