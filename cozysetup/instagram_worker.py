"""The Instagram worker: answers the DMs the webhook recorded (Step 7.4).

The webhook (instagram_webhook.py) only records messages. This worker, in its
own process, takes them in the order they arrived:

  1. Customer messages older than 24 hours are ignored: Instagram no longer
     allows a reply, and the conversation has moved on.
  2. An echo (a message sent from @cozysetup.kw) is either our own reply coming
     back - nothing to do - or the owner writing in the Instagram app: then the
     AI is paused in that conversation until the owner resumes it.
  3. Customer messages are answered by the normal Agent - the same instructions,
     tools, booking rules, language detection and Arabizi guard as everywhere
     else. Several messages sent in a row are answered together, once.
     The first reply in a conversation is preceded by the automated-service
     disclosure. Voice notes, videos, stickers, reels and shares get the
     "unsupported" reply.
  4. The conversation, the replies to send and "done" are saved in one
     transaction - a message is only done once its replies are safely saved.
  5. The saved replies are sent in order through the Instagram sender, with
     retries. Outside Instagram's 24-hour window they become "send yourself"
     for the owner.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cozysetup import outbox
from cozysetup.agent import ConversationLog
from cozysetup.bookings import MAX_PROOF_BYTES, BookingService
from cozysetup.check_instagram import graph_get
from cozysetup.conversations import OWNER_NOTE_PREFIX, ConversationStore, StoredConversation, normalize_username
from cozysetup.database import Actor, HandoffType, InboundStatus, Language, OutboxStatus, ReplyKind, ReplyStatus
from cozysetup.instagram_status import WORKER_HEARTBEAT
from cozysetup.language import detect_language
from cozysetup.replies import render
from cozysetup.senders import INSTAGRAM_CHANNEL, INSTAGRAM_MAX_TEXT_BYTES, INSTAGRAM_WINDOW, SendError, Sender
from cozysetup.settings import MODEL

CHANNEL = INSTAGRAM_CHANNEL
WINDOW = INSTAGRAM_WINDOW              # Instagram: replies only within 24 hours of the customer's last message
MAX_TEXT_BYTES = INSTAGRAM_MAX_TEXT_BYTES   # one text message (Arabic: ~2 bytes a letter)
MAX_ATTEMPTS = 3
# How long to wait after a failed attempt before the next one: after the 1st, after the 2nd.
RETRY_WAITS = (timedelta(minutes=1), timedelta(minutes=5))
DOWNLOAD_TIMEOUT_SECONDS = 20
PROFILE_RETRY = timedelta(minutes=10)   # after a failed username lookup
HEARTBEAT = WORKER_HEARTBEAT
NOT_ON_TEST_LIST = "not on the test list (--only) - not answered by the AI"   # the status counts these

# Everything except images is unsupported (decision D). How it is noted in the conversation:
UNSUPPORTED_NAMES = {
    "audio": "a voice note", "video": "a video", "sticker": "a sticker", "like_heart": "a sticker",
    "ig_reel": "a reel", "reel": "a reel", "share": "a shared post", "story_mention": "a story mention",
    "file": "a file",
}


# --- Reading one recorded message ------------------------------------------------------

@dataclass(frozen=True)
class Incoming:
    row_id: int
    customer_id: str
    attempts: int
    sent_at: datetime            # when it was sent on Instagram
    is_echo: bool                # sent from our account (by us, or by the owner in the app)
    external_id: str             # Instagram's message id
    text: str
    image_urls: tuple[str, ...]
    unsupported: tuple[str, ...]  # attachment types we can't open


def read_incoming(row: sqlite3.Row, timezone) -> Incoming:
    event = json.loads(row["payload"])
    message = event.get("message") or {}
    stamp = event.get("timestamp")
    if isinstance(stamp, (int, float)):
        sent_at = datetime.fromtimestamp(stamp / 1000, timezone)
    else:
        sent_at = datetime.fromisoformat(row["received_at"])

    images, unsupported = [], []
    for attachment in message.get("attachments") or []:
        kind = str(attachment.get("type") or "unknown")
        payload = attachment.get("payload") or {}
        if kind == "image" and payload.get("sticker_id"):
            kind = "sticker"
        if kind == "image" and payload.get("url"):
            images.append(str(payload["url"]))
        else:
            unsupported.append(kind)
    if message.get("is_unsupported"):
        unsupported.append("unsupported")

    return Incoming(
        row_id=row["id"], customer_id=row["channel_user_id"], attempts=row["attempts"], sent_at=sent_at,
        is_echo=bool(message.get("is_echo")), external_id=row["external_id"],
        text=str(message.get("text") or "").strip(), image_urls=tuple(images), unsupported=tuple(unsupported),
    )


def _same_text(ours: str, echoed: str) -> bool:
    """Exactly the same message - ignoring only line-ending style and space at the ends."""
    def normal(text: str) -> str:
        return text.replace("\r\n", "\n").strip()
    return normal(ours) == normal(echoed)


def describe_unsupported(kinds) -> str:
    names = list(dict.fromkeys(UNSUPPORTED_NAMES.get(kind, "a message type that can't be opened") for kind in kinds))
    return ", ".join(names)


# --- Sizes and downloads -----------------------------------------------------------------

def split_text(text: str, limit: int = MAX_TEXT_BYTES) -> list[str]:
    """Split a reply into Instagram messages of at most `limit` bytes - between
    paragraphs if possible, else between lines, else between words."""
    text = text.strip()
    if not text:
        return []
    if _size(text) <= limit:
        return [text]
    for separator in ("\n\n", "\n", " "):
        pieces = text.split(separator)
        if len(pieces) > 1:
            parts, current = [], ""
            for piece in pieces:
                candidate = f"{current}{separator}{piece}" if current else piece
                if _size(candidate) <= limit:
                    current = candidate
                else:
                    if current:
                        parts.append(current)
                    current = piece
            parts.append(current)
            return [small for part in parts for small in split_text(part, limit)]
    parts, current = [], ""          # one very long word: cut between letters
    for letter in text:
        if _size(current + letter) > limit:
            parts.append(current)
            current = letter
        else:
            current += letter
    return parts + [current]


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


class DownloadError(Exception):
    pass


def download_image(url: str) -> bytes:
    """Fetch an image the customer sent (Instagram gives a temporary link)."""
    if not url.startswith("https://"):
        raise DownloadError("the image link is not https")
    request = urllib.request.Request(url, headers={"User-Agent": "CozySetup"})
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_PROOF_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise DownloadError(f"could not download the image (HTTP {error.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DownloadError(f"could not download the image ({getattr(error, 'reason', error)})") from None
    if not data or len(data) > MAX_PROOF_BYTES:
        raise DownloadError(f"the image is empty or larger than {MAX_PROOF_BYTES // (1024 * 1024)} MB")
    return data


class InstagramProfiles:
    """Looks up a customer's @username and name (read-only). Instagram allows this for
    people who have messaged the account."""

    def __init__(self, token: str, get: Callable[[str, dict, str], dict] = graph_get):
        self._token = token
        self._get = get

    def __call__(self, customer_id: str) -> tuple[str | None, str | None]:
        answer = self._get(customer_id, {"fields": "username,name"}, self._token)
        return answer.get("username"), answer.get("name")


# --- The worker ------------------------------------------------------------------------------

@dataclass(frozen=True)
class Outcome:
    customer: str
    what: str       # answered, recorded, echo, owner_replied, ignored, retry, failed,
                    # sent, send_yourself, cancelled, not_answered, marked_sent
    detail: str


class _AlreadyClaimed(Exception):
    """Another worker took these messages first."""


class InstagramWorker:
    def __init__(
        self,
        service: BookingService,
        store: ConversationStore,
        client,
        sender: Sender,
        *,
        download: Callable[[str], bytes] = download_image,
        profiles: Callable[[str], tuple[str | None, str | None]] | None = None,
        only: set[str] | None = None,
        log_dir: Path | None = None,
        model: str = MODEL,
    ):
        """`only`: the @usernames the AI may answer (the test list). None means everyone -
        the command only allows that when asked explicitly. `profiles` looks up usernames."""
        self.service = service
        self.db = service.db
        self.store = store
        self.client = client
        self.sender = sender
        self.download = download
        self.log_dir = log_dir
        self.model = model
        self.profiles = profiles
        self.only = {normalize_username(name) for name in only} if only is not None else None

    def run_once(self) -> list[Outcome]:
        """One round: ignore stale messages, answer the waiting ones, send what's saved."""
        results = self.ignore_stale()
        results += self.process_waiting()
        results += self.send_pending()
        self._heartbeat(results)
        return results

    def may_answer(self, stored: StoredConversation) -> bool:
        """The test list: with --only, the AI answers nobody else - not even someone
        whose username isn't known yet."""
        return self.only is None or (stored.username is not None and stored.username.lower() in self.only)

    # --- 1. Stale messages ----------------------------------------------------------------

    def ignore_stale(self) -> list[Outcome]:
        cutoff, results = self.service.now() - WINDOW, []
        for row in self._waiting_rows():
            incoming = read_incoming(row, self.service.info.timezone)
            # Echoes are never stale: an owner reply must still pause the AI.
            if incoming.is_echo or incoming.sent_at >= cutoff:
                continue
            with self.service.write_transaction():
                self._finish(incoming, InboundStatus.IGNORED, "older than 24 hours - not answered")
            results.append(Outcome(incoming.customer_id, "ignored", "older than 24 hours - not answered"))
        return results

    # --- 2 and 3. Answering ----------------------------------------------------------------

    def process_waiting(self) -> list[Outcome]:
        by_customer: dict[str, list[Incoming]] = {}
        for row in self._waiting_rows():
            incoming = read_incoming(row, self.service.info.timezone)
            by_customer.setdefault(incoming.customer_id, []).append(incoming)

        results = []
        for customer, messages in by_customer.items():
            stored = self._with_profile(self.store.get_or_create(CHANNEL, customer))
            if not self.may_answer(stored):
                results += self._not_on_the_list(stored, messages)
                continue
            # Strictly in order per customer: if one waits for a retry, the later ones wait too.
            position = 0
            while position < len(messages):
                first = messages[position]
                if first.is_echo:
                    batch = [first]
                else:
                    batch = [first]
                    for later in messages[position + 1:]:
                        if later.is_echo:
                            break
                        batch.append(later)
                if not self._retry_wait_over(batch):
                    break
                outcome = self._process_batch(batch)
                if outcome is None:   # another worker has it
                    break
                results.append(outcome)
                if outcome.what in ("retry", "failed"):
                    break
                position += len(batch)
        return results

    def _not_on_the_list(self, stored: StoredConversation, messages: list[Incoming]) -> list[Outcome]:
        """Test mode: the AI never answers this customer. Echoes are still handled (the
        owner replying pauses the AI, as always); customer messages stay waiting for the
        owner - and, like any message, are ignored once older than 24 hours."""
        results = []
        for message in messages:
            if message.is_echo:
                outcome = self._process_batch([message])
                if outcome:
                    results.append(outcome)
                continue
            with self.service.write_transaction():
                newly = self.db.execute("UPDATE inbound_messages SET error = ? WHERE id = ? AND status = ? "
                                        "AND error IS NULL",
                                        (NOT_ON_TEST_LIST, message.row_id, InboundStatus.WAITING.value)).rowcount
            if newly:   # report each message once, not every round
                results.append(Outcome(stored.channel_user_id, "not_answered", f"{stored.who}: {NOT_ON_TEST_LIST}"))
        return results

    def _with_profile(self, stored: StoredConversation) -> StoredConversation:
        """Learn the customer's @username once. A failed lookup never stops a reply;
        it is tried again later."""
        if stored.username or self.profiles is None:
            return stored
        if stored.profile_checked_at and self.service.now() < stored.profile_checked_at + PROFILE_RETRY:
            return stored
        try:
            username, name = self.profiles(stored.channel_user_id)
        except Exception:
            username, name = None, None
        return self.store.set_profile(stored, username, name)

    def _process_batch(self, batch: list[Incoming]) -> Outcome | None:
        try:
            self._claim(batch)
        except _AlreadyClaimed:
            return None
        try:
            if batch[0].is_echo:
                return self._handle_echo(batch[0])
            return self._handle_customer(batch)
        except Exception as error:
            return self._attempt_failed(batch, error)

    def _handle_echo(self, echo: Incoming) -> Outcome:
        # Ours: a chat reply this worker sent, or a confirmation/reminder the outbox sent.
        ours = self.db.execute(
            "SELECT 1 FROM conversation_replies WHERE external_id = ? UNION ALL SELECT 1 FROM outbox WHERE external_id = ?",
            (echo.external_id, echo.external_id)).fetchone()
        if ours:
            with self.service.write_transaction():
                self._finish(echo, InboundStatus.IGNORED, "echo of our own reply")
            return Outcome(echo.customer_id, "echo", "our own reply came back - nothing to do")
        if self._is_system_message(echo):
            with self.service.write_transaction():
                self._finish(echo, InboundStatus.IGNORED, "a confirmation or reminder (matched by its text)")
            marked = self._mark_sent_by_owner(echo)
            if marked:
                return Outcome(echo.customer_id, "marked_sent",
                               f"the owner sent {marked} in Instagram - marked as sent; the AI keeps answering")
            return Outcome(echo.customer_id, "echo", "a confirmation or reminder - the AI keeps answering")

        # The owner wrote to the customer in the Instagram app: the AI steps back (decision C).
        stored = self.store.get_or_create(CHANNEL, echo.customer_id)
        agent = self.store.open_agent(self.client, stored, model=self.model)
        what = echo.text or describe_unsupported(echo.unsupported) or "an image"
        agent.messages.append({"role": "assistant", "content": f"{OWNER_NOTE_PREFIX}{what})"})
        now = self._stamp()

        def also():
            self.db.execute(
                "UPDATE conversations SET ai_paused = 1, paused_at = COALESCE(paused_at, ?), "
                "pause_reason = COALESCE(pause_reason, ?), updated_at = ? WHERE id = ?",
                (now, "The owner replied in Instagram", now, stored.id))
            self.db.execute(
                "UPDATE conversation_replies SET status = ?, last_error = ?, updated_at = ? "
                "WHERE conversation_id = ? AND status = ?",
                (ReplyStatus.CANCELLED.value, "the owner took over the conversation", now, stored.id,
                 ReplyStatus.PENDING.value))
            self._finish(echo, InboundStatus.DONE)

        self.store.save_agent(stored, agent, also)
        return Outcome(echo.customer_id, "owner_replied", "the owner replied in Instagram - AI paused")

    def _is_system_message(self, echo: Incoming) -> bool:
        """An echo whose id we don't know, but whose text is exactly one of this customer's
        confirmations or reminders: the owner sent a "send yourself" message in the app, or
        the outbox sent it and hasn't saved Instagram's id yet (or crashed before saving it).
        That is not the owner taking over the conversation, so the AI isn't paused.
        The same for a chat reply of ours still being sent. Cancelled messages don't count."""
        if not echo.text:
            return False
        texts = self.db.execute(
            "SELECT text FROM outbox WHERE channel = ? AND recipient = ? AND status IN (?, ?, ?, ?) "
            "UNION ALL SELECT r.text FROM conversation_replies r JOIN conversations c ON c.id = r.conversation_id "
            "WHERE c.channel = ? AND c.channel_user_id = ? AND r.status = ?",
            (CHANNEL, echo.customer_id, OutboxStatus.PENDING.value, OutboxStatus.SENT.value,
             OutboxStatus.FAILED.value, OutboxStatus.SEND_YOURSELF.value,
             CHANNEL, echo.customer_id, ReplyStatus.PENDING.value)).fetchall()
        return any(_same_text(row["text"], echo.text) for row in texts)

    def _mark_sent_by_owner(self, echo: Incoming) -> str | None:
        """The echo is word for word one of this customer's "send yourself" confirmations
        or reminders: the owner has sent it - record that (decision: automatic mark-sent)."""
        rows = self.db.execute("SELECT id, kind, text FROM outbox WHERE channel = ? AND recipient = ? AND status = ? "
                               "ORDER BY id", (CHANNEL, echo.customer_id, OutboxStatus.SEND_YOURSELF.value)).fetchall()
        for row in rows:
            if _same_text(row["text"], echo.text) and outbox.mark_sent_from_echo(self.service, row["id"],
                                                                                  echo.external_id):
                return f"the {row['kind']} (#{row['id']})"
        return None

    def _handle_customer(self, batch: list[Incoming]) -> Outcome:
        stored = self.store.get_or_create(CHANNEL, batch[0].customer_id)
        latest = max(message.sent_at for message in batch)
        if stored.last_customer_message_at and stored.last_customer_message_at > latest:
            latest = stored.last_customer_message_at
        texts = [message.text for message in batch if message.text]
        urls = [url for message in batch for url in message.image_urls]
        unsupported = [kind for message in batch for kind in message.unsupported]
        agent = self.store.open_agent(self.client, stored, model=self.model, log=self._log(stored))

        if stored.ai_paused:
            # The owner is handling this conversation: keep the messages for later, don't answer.
            notes = texts + ["[Customer sent an image]" for _ in urls]
            if unsupported:
                notes.append(f"[Customer sent {describe_unsupported(unsupported)}]")
            agent.messages.append({"role": "user", "content": "\n".join(notes)})
            self.store.save_agent(stored, agent, lambda: self._record_turn(stored, batch, latest, []))
            return Outcome(stored.channel_user_id, "recorded",
                           f"{len(batch)} message(s) kept - the AI is paused, the owner is handling this chat")

        images = [self.download(url) for url in urls]   # a failure here is retried later
        language = detect_language(" ".join(texts)) or stored.language
        replies: list[tuple[ReplyKind, str]] = []

        if not stored.disclosure_sent:   # decision B: before the first reply of each conversation
            disclosure = self._fixed("automated_disclosure", language)
            agent.messages.append({"role": "assistant", "content": disclosure})
            replies.append((ReplyKind.DISCLOSURE, disclosure))

        details = []
        if texts or images:
            answer = agent.reply("\n".join(texts), images=images)
            replies.append((ReplyKind.REPLY, answer.text))
            if answer.tools_used:
                details.append("tools: " + ", ".join(answer.tools_used))
            if answer.handed_off_after_problem:
                details.append("a problem - handed to the owner")
            details.append(f"about ${answer.usage.cost_usd(self.model):.4f}")
        if unsupported:   # decision D
            agent.messages.append({"role": "user", "content": f"[Customer sent {describe_unsupported(unsupported)}, "
                                                              "which can't be opened]"})
            text = self._fixed("unsupported_message", agent.customer_language or language)
            agent.messages.append({"role": "assistant", "content": text})
            replies.append((ReplyKind.UNSUPPORTED, text))

        self.store.save_agent(stored, agent, lambda: self._record_turn(stored, batch, latest, replies))
        kinds = ", ".join(kind.value for kind, _ in replies)
        return Outcome(stored.channel_user_id, "answered",
                       f"{len(batch)} message(s) → {kinds}" + (f" ({'; '.join(details)})" if details else ""))

    def _record_turn(self, stored: StoredConversation, batch: list[Incoming], latest: datetime,
                     replies: list[tuple[ReplyKind, str]]) -> None:
        """Inside the conversation's save transaction: the replies to send, the
        conversation facts, and "done" for every message answered."""
        now = self._stamp()
        for kind, text in replies:
            for part in split_text(text):
                self.db.execute(
                    "INSERT INTO conversation_replies (conversation_id, kind, text, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (stored.id, kind.value, part, ReplyStatus.PENDING.value, now, now))
        disclosure = any(kind is ReplyKind.DISCLOSURE for kind, _ in replies)
        self.db.execute(
            "UPDATE conversations SET last_customer_message_at = ?, disclosure_sent = MAX(disclosure_sent, ?), "
            "updated_at = ? WHERE id = ?",
            (latest.isoformat(timespec="seconds"), int(disclosure), now, stored.id))
        for message in batch:
            self._finish(message, InboundStatus.DONE)

    # --- Claims, retries and failures -----------------------------------------------------------

    def _claim(self, batch: list[Incoming]) -> None:
        """Count this attempt before starting, so two workers never answer the same message."""
        with self.service.write_transaction():
            for message in batch:
                claimed = self.db.execute(
                    "UPDATE inbound_messages SET attempts = attempts + 1, last_attempt_at = ? "
                    "WHERE id = ? AND status = ? AND attempts = ?",
                    (self._stamp(), message.row_id, InboundStatus.WAITING.value, message.attempts)).rowcount
                if not claimed:
                    raise _AlreadyClaimed

    def _retry_wait_over(self, batch: list[Incoming]) -> bool:
        now = self.service.now()
        for message in batch:
            if message.attempts == 0:
                continue
            row = self.db.execute("SELECT last_attempt_at FROM inbound_messages WHERE id = ?",
                                  (message.row_id,)).fetchone()
            wait = RETRY_WAITS[min(message.attempts, len(RETRY_WAITS)) - 1]
            if row["last_attempt_at"] and now < datetime.fromisoformat(row["last_attempt_at"]) + wait:
                return False
        return True

    def _attempt_failed(self, batch: list[Incoming], error: Exception) -> Outcome:
        """Nothing of this attempt was saved. Try again later - or, after the last
        attempt, give up and hand the conversation to the owner."""
        attempt = max(message.attempts for message in batch) + 1
        reason = f"{type(error).__name__}: {error}"
        customer = batch[0].customer_id
        if attempt < MAX_ATTEMPTS:
            with self.service.write_transaction():
                for message in batch:
                    self.db.execute("UPDATE inbound_messages SET error = ? WHERE id = ?", (reason, message.row_id))
            wait = int(RETRY_WAITS[attempt - 1].total_seconds() // 60)
            return Outcome(customer, "retry", f"attempt {attempt} failed ({reason}) - next try in {wait} min")

        with self.service.write_transaction():
            for message in batch:
                self._finish(message, InboundStatus.FAILED, reason)
        said = [message.text for message in batch if message.text] or ["(no text)"]
        handoff = self.service.create_handoff(
            HandoffType.OTHER,
            f"Could not answer this Instagram customer after {attempt} attempts (last error: {reason}). "
            f"Their message(s): {' / '.join(said)!r}. Please reply to them yourself in Instagram.",
            channel=CHANNEL, channel_user_id=customer, actor=Actor.SYSTEM,
        )
        return Outcome(customer, "failed", f"{reason} - handoff #{handoff.id} created")

    def _finish(self, message: Incoming, status: InboundStatus, note: str | None = None) -> None:
        self.db.execute("UPDATE inbound_messages SET status = ?, processed_at = ?, error = ? WHERE id = ?",
                        (status.value, self._stamp(), note, message.row_id))

    # --- 5. Sending ---------------------------------------------------------------------------------

    def send_pending(self) -> list[Outcome]:
        rows = self.db.execute(
            "SELECT r.*, c.channel_user_id, c.ai_paused, c.last_customer_message_at, c.username "
            "FROM conversation_replies r JOIN conversations c ON c.id = r.conversation_id "
            "WHERE r.status = ? AND c.channel = ? ORDER BY r.id",
            (ReplyStatus.PENDING.value, CHANNEL)).fetchall()
        results, stopped = [], set()
        for row in rows:
            if row["conversation_id"] in stopped:   # keep the order: nothing overtakes a waiting reply
                continue
            if self.only is not None and (row["username"] or "").lower() not in self.only:
                stopped.add(row["conversation_id"])   # test mode: never an AI reply to anyone else
                continue
            try:
                outcome = self._send_one(row)
            except Exception as error:
                # Something unexpected with this one reply (e.g. the database was busy too long).
                # It stays pending and is tried again later; other conversations carry on.
                outcome = Outcome(row["channel_user_id"], "retry", f"{type(error).__name__}: {error}")
            if outcome is None:
                stopped.add(row["conversation_id"])
                continue
            results.append(outcome)
            if outcome.what not in ("sent",):
                stopped.add(row["conversation_id"])
        return results

    def _send_one(self, row: sqlite3.Row) -> Outcome | None:
        customer, now = row["channel_user_id"], self.service.now()
        stamp = now.isoformat(timespec="seconds")

        if row["ai_paused"]:
            with self.service.write_transaction():
                texts = self._stop_conversation_replies(row, ReplyStatus.CANCELLED,
                                                        "the owner took over the conversation")
            return Outcome(customer, "cancelled",
                           f"{len(texts)} reply part(s) not sent - the owner took over this conversation")

        last = row["last_customer_message_at"]
        if not last or now >= datetime.fromisoformat(last) + WINDOW:
            return self._outside_window(row)

        if row["attempts"]:
            wait = RETRY_WAITS[min(row["attempts"], len(RETRY_WAITS)) - 1]
            if now < datetime.fromisoformat(row["updated_at"]) + wait:
                return None

        with self.service.write_transaction():
            claimed = self.db.execute(
                "UPDATE conversation_replies SET attempts = attempts + 1, updated_at = ? "
                "WHERE id = ? AND status = ? AND attempts = ?",
                (stamp, row["id"], ReplyStatus.PENDING.value, row["attempts"])).rowcount
        if not claimed:
            return None

        attempt = row["attempts"] + 1
        message_id, error = None, None
        try:
            message_id = self.sender.send(customer, row["text"])
        except SendError as problem:
            error = problem
        except Exception as problem:   # a bug in the sender: treat it as temporary
            error = SendError(f"{type(problem).__name__}: {problem}")

        with self.service.write_transaction():
            status = self.db.execute("SELECT status FROM conversation_replies WHERE id = ?",
                                     (row["id"],)).fetchone()["status"]
            if status != ReplyStatus.PENDING.value:
                return Outcome(customer, "cancelled", "cancelled while sending")
            if error is None:
                self.db.execute(
                    "UPDATE conversation_replies SET status = ?, external_id = ?, sent_at = ?, last_error = NULL, "
                    "updated_at = ? WHERE id = ?",
                    (ReplyStatus.SENT.value, message_id, stamp, stamp, row["id"]))
                return Outcome(customer, "sent", f"{row['kind']} sent")
            if not error.permanent and attempt < MAX_ATTEMPTS:
                self.db.execute("UPDATE conversation_replies SET last_error = ? WHERE id = ?",
                                (str(error), row["id"]))
                wait = int(RETRY_WAITS[attempt - 1].total_seconds() // 60)
                return Outcome(customer, "retry", f"{row['kind']} attempt {attempt} failed ({error}) "
                                                  f"- next try in {wait} min")
            texts = self._stop_conversation_replies(row, ReplyStatus.FAILED, str(error))
        handoff = self._handoff_unsent(customer, f"could not be sent via Instagram ({error})", texts)
        return Outcome(customer, "failed", f"{error} - handoff #{handoff.id} created")

    def _outside_window(self, row: sqlite3.Row) -> Outcome:
        """Decision A: Instagram's 24-hour window has closed - the owner sends it personally."""
        reason = "outside Instagram's 24-hour reply window"
        with self.service.write_transaction():
            texts = self._stop_conversation_replies(row, ReplyStatus.SEND_YOURSELF, reason)
        handoff = self._handoff_unsent(row["channel_user_id"], f"was not sent: {reason}", texts)
        return Outcome(row["channel_user_id"], "send_yourself", f"{reason} - handoff #{handoff.id} created")

    def _stop_conversation_replies(self, row: sqlite3.Row, status: ReplyStatus, reason: str) -> list[str]:
        """This reply and the ones after it in the conversation: they belong together."""
        rows = self.db.execute(
            "SELECT id, text FROM conversation_replies WHERE conversation_id = ? AND status = ? AND id >= ? "
            "ORDER BY id", (row["conversation_id"], ReplyStatus.PENDING.value, row["id"])).fetchall()
        for reply in rows:
            self._set_reply(reply["id"], status, reason)
        return [reply["text"] for reply in rows]

    def _set_reply(self, reply_id: int, status: ReplyStatus, reason: str) -> None:
        self.db.execute("UPDATE conversation_replies SET status = ?, last_error = ?, updated_at = ? "
                        "WHERE id = ? AND status = ?",
                        (status.value, reason, self._stamp(), reply_id, ReplyStatus.PENDING.value))

    def _handoff_unsent(self, customer: str, problem: str, texts: list[str]):
        quoted = "\n\n".join(texts)
        return self.service.create_handoff(
            HandoffType.OTHER,
            f"A reply to this Instagram customer {problem}. Please send it yourself in Instagram "
            f"if it is still useful:\n\n{quoted}",
            channel=CHANNEL, channel_user_id=customer, actor=Actor.SYSTEM,
        )

    # --- Helpers ------------------------------------------------------------------------------------

    def _heartbeat(self, results: list[Outcome]) -> None:
        counts: dict[str, int] = {}
        for result in results:
            counts[result.what] = counts.get(result.what, 0) + 1
        details = ", ".join(f"{what} {count}" for what, count in sorted(counts.items())) or "nothing to do"
        with self.service.write_transaction():
            self.db.execute("INSERT INTO heartbeats (program, last_round_at, details) VALUES (?, ?, ?) "
                            "ON CONFLICT (program) DO UPDATE SET last_round_at = excluded.last_round_at, "
                            "details = excluded.details", (HEARTBEAT, self._stamp(), details))

    def _waiting_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM inbound_messages WHERE channel = ? AND status = ? ORDER BY id",
                               (CHANNEL, InboundStatus.WAITING.value)).fetchall()

    def _fixed(self, name: str, language: Language | None) -> str:
        """One of the owner's fixed replies - Arabic once the owner has written it, else English."""
        texts = render(self.service.info, name)
        return texts["ar"] if language is Language.ARABIC and "ar" in texts else texts["en"]

    def _log(self, stored: StoredConversation) -> ConversationLog | None:
        return ConversationLog(self.log_dir / f"instagram_{stored.id}.jsonl") if self.log_dir else None

    def _stamp(self) -> str:
        return self.service.now().isoformat(timespec="seconds")
