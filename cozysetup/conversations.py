"""Keep customer conversations on real channels (Instagram) in the database.

On Instagram every DM arrives on its own, to a server that can restart. So after
each reply the conversation is saved, and before the next one it is loaded again
into a normal Agent. The Agent itself doesn't change: the store only fills in and
reads back what the Agent already keeps - history, language, last booking summary,
and the images the customer sent.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from cozysetup.agent import EFFORT, Agent, ConversationLog
from cozysetup.bookings import BookingPreview, BookingService, detect_proof_file_type
from cozysetup.database import DEFAULT_DB_PATH, Language
from cozysetup.rules import Amounts, PaymentChoice
from cozysetup.settings import MODEL
from cozysetup.tools import Conversation

DEFAULT_ATTACHMENTS_DIR = DEFAULT_DB_PATH.parent / "conversation_attachments"


@dataclass(frozen=True)
class StoredConversation:
    id: int
    channel: str
    channel_user_id: str
    language: Language | None
    disclosure_sent: bool
    ai_paused: bool
    paused_at: datetime | None
    pause_reason: str | None
    last_customer_message_at: datetime | None
    created_at: datetime
    updated_at: datetime
    username: str | None = None          # Instagram @username, without the @
    display_name: str | None = None
    profile_checked_at: datetime | None = None

    @property
    def who(self) -> str:
        """How the owner recognises the customer, e.g. "@sara.k (Sara)"."""
        if self.username:
            return f"@{self.username}" + (f" ({self.display_name})" if self.display_name else "")
        return f"Instagram id {self.channel_user_id} (username not known yet)"

    @classmethod
    def from_row(cls, row) -> StoredConversation:
        def moment(value):
            return datetime.fromisoformat(value) if value else None
        return cls(
            id=row["id"], channel=row["channel"], channel_user_id=row["channel_user_id"],
            language=Language(row["language"]) if row["language"] else None,
            disclosure_sent=bool(row["disclosure_sent"]), ai_paused=bool(row["ai_paused"]),
            paused_at=moment(row["paused_at"]), pause_reason=row["pause_reason"],
            last_customer_message_at=moment(row["last_customer_message_at"]),
            created_at=moment(row["created_at"]), updated_at=moment(row["updated_at"]),
            username=row["username"], display_name=row["display_name"],
            profile_checked_at=moment(row["profile_checked_at"]),
        )


class ConversationStore:
    def __init__(self, service: BookingService, attachments_dir: Path = DEFAULT_ATTACHMENTS_DIR):
        self.service = service
        self.db = service.db
        self.attachments_dir = attachments_dir

    # --- Finding a customer's conversation ------------------------------------------

    def get(self, channel: str, channel_user_id: str) -> StoredConversation | None:
        row = self.db.execute("SELECT * FROM conversations WHERE channel = ? AND channel_user_id = ?",
                              (channel, channel_user_id)).fetchone()
        return StoredConversation.from_row(row) if row else None

    def get_or_create(self, channel: str, channel_user_id: str) -> StoredConversation:
        with self.service.write_transaction():
            stamp = self._stamp()
            # The (channel, customer) pair is unique: a second message never makes a second conversation.
            self.db.execute(
                "INSERT OR IGNORE INTO conversations (channel, channel_user_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)", (channel, channel_user_id, stamp, stamp))
        return self.get(channel, channel_user_id)

    def find(self, channel: str, who: str) -> StoredConversation | None:
        """By @username (with or without the @, any capitals) or by the channel's id."""
        who = who.strip()
        row = self.db.execute("SELECT * FROM conversations WHERE channel = ? AND username = ? COLLATE NOCASE",
                              (channel, normalize_username(who))).fetchone()
        if row is None:
            row = self.db.execute("SELECT * FROM conversations WHERE channel = ? AND channel_user_id = ?",
                                  (channel, who)).fetchone()
        return StoredConversation.from_row(row) if row else None

    def all(self, channel: str) -> list[StoredConversation]:
        """Most recently active first."""
        rows = self.db.execute("SELECT * FROM conversations WHERE channel = ? ORDER BY updated_at DESC, id DESC",
                               (channel,))
        return [StoredConversation.from_row(row) for row in rows]

    def set_profile(self, stored: StoredConversation, username: str | None,
                    display_name: str | None) -> StoredConversation:
        """What Instagram said about the customer (None when it couldn't say)."""
        with self.service.write_transaction():
            stamp = self._stamp()
            self.db.execute(
                "UPDATE conversations SET username = COALESCE(?, username), display_name = COALESCE(?, display_name), "
                "profile_checked_at = ? WHERE id = ?",
                (normalize_username(username) if username else None, display_name, stamp, stored.id))
        return self.reload(stored)

    def transcript(self, stored: StoredConversation) -> list[tuple[str, str]]:
        """The conversation as the owner reads it: (who, text) - customer, AI or owner."""
        row = self.db.execute("SELECT history FROM conversations WHERE id = ?", (stored.id,)).fetchone()
        return readable_history(json.loads(row["history"]))

    def reload(self, stored: StoredConversation) -> StoredConversation:
        return StoredConversation.from_row(
            self.db.execute("SELECT * FROM conversations WHERE id = ?", (stored.id,)).fetchone())

    # --- Loading a conversation into an Agent, and saving it back ----------------------

    def open_agent(self, client, stored: StoredConversation, *, model: str = MODEL, effort: str = EFFORT,
                   log: ConversationLog | None = None) -> Agent:
        """A normal Agent that continues exactly where the conversation left off."""
        row = self.db.execute("SELECT history, last_preview FROM conversations WHERE id = ?",
                              (stored.id,)).fetchone()
        conversation = Conversation(channel=stored.channel, channel_user_id=stored.channel_user_id)
        conversation.attachments = self._load_attachments(stored.id)
        conversation.last_preview = preview_from_json(row["last_preview"])
        agent = Agent(client, self.service, conversation, model=model, effort=effort, log=log)
        agent.messages = json.loads(row["history"])
        agent.customer_language = stored.language
        return agent

    def save_agent(self, stored: StoredConversation, agent: Agent,
                   also: Callable[[], None] | None = None) -> StoredConversation:
        """Save everything the Agent needs to continue later. New images are written
        to files first, then everything is recorded in one transaction.

        `also` runs inside that same transaction - e.g. the Instagram worker saves the
        replies to send and marks the customer's messages done: all of it, or none."""
        known = {row["number"] for row in self.db.execute(
            "SELECT number FROM conversation_attachments WHERE conversation_id = ?", (stored.id,))}
        new_files = []
        for number, data in agent.conversation.attachments.items():
            if number not in known:
                name = f"{number}.{detect_proof_file_type(data) or 'bin'}"
                folder = self.attachments_dir / str(stored.id)
                folder.mkdir(parents=True, exist_ok=True)
                (folder / name).write_bytes(data)
                new_files.append((number, name))

        with self.service.write_transaction():
            stamp = self._stamp()
            for number, name in new_files:
                self.db.execute(
                    "INSERT INTO conversation_attachments (conversation_id, number, file_name, created_at) "
                    "VALUES (?, ?, ?, ?)", (stored.id, number, name, stamp))
            language = agent.customer_language.value if agent.customer_language else None
            self.db.execute(
                "UPDATE conversations SET history = ?, last_preview = ?, language = ?, updated_at = ? WHERE id = ?",
                (history_to_json(agent.messages), preview_to_json(agent.conversation.last_preview),
                 language, stamp, stored.id))
            if also:
                also()
        return self.reload(stored)

    # --- Facts about the conversation ------------------------------------------------------

    def mark_customer_message(self, stored: StoredConversation, at: datetime) -> StoredConversation:
        """The customer just wrote: the 24-hour messaging window starts again."""
        return self._update(stored, "last_customer_message_at = ?", at.isoformat(timespec="seconds"))

    def mark_disclosure_sent(self, stored: StoredConversation) -> StoredConversation:
        return self._update(stored, "disclosure_sent = ?", 1)

    def pause_ai(self, stored: StoredConversation, reason: str) -> StoredConversation:
        """The owner took over: the AI stays quiet in this conversation until resumed."""
        with self.service.write_transaction():
            stamp = self._stamp()
            self.db.execute("UPDATE conversations SET ai_paused = 1, paused_at = ?, pause_reason = ?, "
                            "updated_at = ? WHERE id = ?", (stamp, reason, stamp, stored.id))
        return self.reload(stored)

    def resume_ai(self, stored: StoredConversation) -> StoredConversation:
        with self.service.write_transaction():
            self.db.execute("UPDATE conversations SET ai_paused = 0, paused_at = NULL, pause_reason = NULL, "
                            "updated_at = ? WHERE id = ?", (self._stamp(), stored.id))
        return self.reload(stored)

    # --- Helpers --------------------------------------------------------------------------------

    def _update(self, stored: StoredConversation, assignment: str, value) -> StoredConversation:
        with self.service.write_transaction():
            self.db.execute(f"UPDATE conversations SET {assignment}, updated_at = ? WHERE id = ?",
                            (value, self._stamp(), stored.id))
        return self.reload(stored)

    def _load_attachments(self, conversation_id: int) -> dict[int, bytes]:
        rows = self.db.execute("SELECT number, file_name FROM conversation_attachments "
                               "WHERE conversation_id = ? ORDER BY number", (conversation_id,))
        return {row["number"]: (self.attachments_dir / str(conversation_id) / row["file_name"]).read_bytes()
                for row in rows}

    def _stamp(self) -> str:
        return self.service.now().isoformat(timespec="seconds")


# --- Reading a conversation --------------------------------------------------------------------

OWNER_NOTE_PREFIX = "(The owner replied personally: "


def normalize_username(username: str) -> str:
    return username.strip().lstrip("@").strip().lower()


def readable_history(history: list) -> list[tuple[str, str]]:
    """Customer and AI messages, and the owner's own replies - without the AI's
    internal steps (reasoning, tool calls and their results)."""
    lines = []
    for item in history:
        if not isinstance(item, dict):
            continue
        if item.get("role") == "user" and isinstance(item.get("content"), str):
            lines.append(("customer", item["content"]))
        elif item.get("role") == "assistant" and isinstance(item.get("content"), str):
            text = item["content"]
            if text.startswith(OWNER_NOTE_PREFIX) and text.endswith(")"):
                lines.append(("owner", text[len(OWNER_NOTE_PREFIX):-1]))
            else:
                lines.append(("ai", text))
        elif item.get("type") == "message":
            said = "\n\n".join(part.get("text", "") for part in item.get("content") or []
                                if isinstance(part, dict) and part.get("type") == "output_text").strip()
            if said:
                lines.append(("ai", said))
        elif item.get("type") == "function_call":
            lines.append(("tool", str(item.get("name"))))
    return lines


# --- Turning what the Agent keeps into JSON and back ----------------------------------------

def history_to_json(messages: list) -> str:
    """The AI conversation as JSON. Items from the OpenAI library (messages, tool calls,
    reasoning with its encrypted content) become plain data, which OpenAI accepts back."""
    return json.dumps([_plain(item) for item in messages], ensure_ascii=False)


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if hasattr(value, "__dict__"):
        return {key: _plain(item) for key, item in vars(value).items()}
    return value


def preview_to_json(preview: BookingPreview | None) -> str | None:
    if preview is None:
        return None
    return json.dumps({
        "booking_date": preview.booking_date.isoformat(), "location_id": preview.location_id,
        "customer_name": preview.customer_name, "customer_phone": preview.customer_phone,
        "payment_choice": preview.payment_choice.value,
        "amounts": {"rental_price": preview.amounts.rental_price, "amount_now": preview.amounts.amount_now,
                    "remaining_on_day": preview.amounts.remaining_on_day,
                    "security_deposit": preview.amounts.security_deposit},
    }, ensure_ascii=False)


def preview_from_json(text: str | None) -> BookingPreview | None:
    if not text:
        return None
    data = json.loads(text)
    return BookingPreview(
        booking_date=date.fromisoformat(data["booking_date"]), location_id=data["location_id"],
        customer_name=data["customer_name"], customer_phone=data["customer_phone"],
        payment_choice=PaymentChoice(data["payment_choice"]), amounts=Amounts(**data["amounts"]),
    )
