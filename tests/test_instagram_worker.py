"""Tests for the Instagram worker (Step 7.4): recorded DMs -> the Agent -> replies sent.

A pretend OpenAI model, a pretend Instagram sender and pretend image downloads -
no API calls, no messages sent, no cost. Messages are recorded with the real
webhook code, exactly as Meta's events would be.
"""

import itertools
import json
import sqlite3
import urllib.error
from datetime import datetime, timedelta

import httpx2
import openai
import pytest

from cozysetup import instagram_worker, senders
from cozysetup.bookings import MAX_PROOF_BYTES, BookingService
from cozysetup.business_info import load_business_info
from cozysetup.conversations import ConversationStore
from cozysetup.database import MIGRATIONS, SCHEMA_V1, SCHEMA_VERSION, BookingStatus, HandoffType, connect
from cozysetup.instagram_webhook import read_events, store
from cozysetup.instagram_worker import (
    MAX_TEXT_BYTES,
    DownloadError,
    InstagramWorker,
    _AlreadyClaimed,
    download_image,
    read_incoming,
    split_text,
)
from cozysetup.replies import render
from cozysetup.senders import GraphError, InstagramSender, SendError
from cozysetup.serve_instagram import main
from tests.test_agent import BOOKING, PretendOpenAI, answer, call, text

INFO = load_business_info()
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=INFO.timezone)
ACCOUNT = "17841461464339502"
CUSTOMER = "1234567890123456"
OTHER_CUSTOMER = "6543210987654321"
DISCLOSURE = render(INFO, "automated_disclosure")["en"]
UNSUPPORTED = render(INFO, "unsupported_message")["en"]
HANDOFF = render(INFO, "handoff")["en"]
JPG = b"\xff\xd8\xff\xe0" + bytes(100)
SENT_IDS = itertools.count(1)   # Instagram's message ids never repeat - not even across restarts


# --- Pretend Instagram ------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, **amount):
        self.now += timedelta(**amount)


class PretendSender:
    """Records what would have been sent. `errors`: one entry per send - an error to raise, or None."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.sent = []

    def send(self, recipient, message):
        if self.errors:
            error = self.errors.pop(0)
            if error:
                raise error
        self.sent.append((recipient, message))
        return f"mid.ours.{next(SENT_IDS)}"


class Downloads:
    def __init__(self, *results):
        self.results = list(results)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        result = self.results.pop(0) if self.results else JPG
        if isinstance(result, Exception):
            raise result
        return result


class Setup:
    def __init__(self, path, client=None, sender=None, download=None):
        self.path = path
        self.clock = Clock()
        self.db = connect(path)
        self.service = BookingService(self.db, INFO, clock=self.clock, proofs_dir=path.parent / "proofs")
        self.store = ConversationStore(self.service, path.parent / "attachments")
        self.client = client or PretendOpenAI()
        self.sender = sender or PretendSender()
        self.download = download or Downloads()
        self.worker = InstagramWorker(self.service, self.store, self.client, self.sender,
                                      download=self.download, log_dir=path.parent / "logs")
        self.mids = 0

    def receive(self, *events, at=None):
        """The webhook records the events, exactly like a real delivery from Meta."""
        payload = {"object": "instagram", "entry": [{"id": ACCOUNT, "time": 1, "messaging": list(events)}]}
        store(self.db, read_events(payload, ACCOUNT).messages, at or self.clock())

    def dm(self, words=None, *, customer=CUSTOMER, attachments=None, echo=False, mid=None, at=None):
        self.mids += 1
        message = {"mid": mid or f"mid.{self.mids}"}
        if words is not None:
            message["text"] = words
        if attachments:
            message["attachments"] = attachments
        if echo:
            message["is_echo"] = True
        sender, recipient = (ACCOUNT, customer) if echo else (customer, ACCOUNT)
        return {"sender": {"id": sender}, "recipient": {"id": recipient},
                "timestamp": int((at or self.clock()).timestamp() * 1000), "message": message}

    def inbound(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM inbound_messages ORDER BY id")]

    def replies(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM conversation_replies ORDER BY id")]

    def conversation(self, customer=CUSTOMER):
        return self.store.get("instagram", customer)

    def history(self, customer=CUSTOMER):
        row = self.db.execute("SELECT history FROM conversations WHERE channel_user_id = ?", (customer,)).fetchone()
        return json.loads(row["history"])

    def close(self):
        self.db.close()


@pytest.fixture
def make(tmp_path):
    made = []

    def build(**parts):
        setup = Setup(tmp_path / "cozysetup.db", **parts)
        made.append(setup)
        return setup

    yield build
    for setup in made:
        setup.close()


def image(url="https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1"):
    return [{"type": "image", "payload": {"url": url}}]


def voice_note():
    return [{"type": "audio", "payload": {"url": "https://lookaside.fbsbx.com/a"}}]


# --- Answering a message ------------------------------------------------------------------------

def test_a_first_message_gets_the_disclosure_then_the_ai_reply(make):
    s = make(client=PretendOpenAI(answer(text("Hello! How can I help?"))))
    s.receive(s.dm("Hi, how much is the setup?"))
    results = s.worker.run_once()

    assert s.sender.sent == [(CUSTOMER, DISCLOSURE), (CUSTOMER, "Hello! How can I help?")]
    assert [r.what for r in results] == ["answered", "sent", "sent"]
    [message] = s.inbound()
    assert (message["status"], message["processed_at"]) == ("done", NOW.isoformat(timespec="seconds"))
    assert [(r["kind"], r["status"]) for r in s.replies()] == [("disclosure", "sent"), ("reply", "sent")]
    assert all(r["external_id"].startswith("mid.ours.") for r in s.replies())
    stored = s.conversation()
    assert stored.disclosure_sent and stored.last_customer_message_at == NOW
    # The customer's words reached the model; the disclosure is in the history it sees.
    request = s.client.requests[0]["input"]
    assert request[0] == {"role": "assistant", "content": DISCLOSURE}
    assert request[1] == {"role": "user", "content": "Hi, how much is the setup?"}


def test_the_disclosure_is_sent_only_once_per_conversation(make):
    s = make(client=PretendOpenAI(answer(text("Hello!")), answer(text("It is 50 KWD."))))
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    s.receive(s.dm("How much?"))
    s.worker.run_once()
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, "Hello!", "It is 50 KWD."]
    # The second request continued the same conversation.
    assert [m["content"] for m in s.client.requests[1]["input"] if isinstance(m, dict) and m.get("role") == "user"] \
        == ["Hi", "How much?"]


def test_messages_sent_in_a_row_are_answered_once_together(make):
    s = make(client=PretendOpenAI(answer(text("Sure - which date?"))))
    s.receive(s.dm("Hi"), s.dm("I want to book"), s.dm("in Julaia"))
    s.worker.run_once()
    assert len(s.client.requests) == 1
    assert s.client.requests[0]["input"][-1] == {"role": "user", "content": "Hi\nI want to book\nin Julaia"}
    assert {m["status"] for m in s.inbound()} == {"done"}
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, "Sure - which date?"]


def test_the_booking_flow_works_across_messages_with_the_normal_tools_and_rules(make):
    s = make(client=PretendOpenAI(
        answer(call("show_booking_summary", **BOOKING)), answer(text("Please check your booking details..."))))
    s.receive(s.dm("Julaia, 1 October, deposit, Ahmad, 99999999"))
    s.worker.run_once()
    s.close()

    # A restart between the summary and the "yes", like a real server.
    s = make(client=PretendOpenAI(
        answer(call("create_booking", call_id="call_2", **BOOKING, customer_language="en")),
        answer(text("Your booking CS-0001 has been created."))))
    s.receive(s.dm("yes", mid="mid.yes"))   # a new message id (the first run used mid.1)
    s.worker.run_once()

    booking = s.service.get_booking("CS-0001")
    assert booking.status is BookingStatus.PENDING_PAYMENT          # never confirmed by the AI
    assert (booking.channel, booking.channel_user_id) == ("instagram", CUSTOMER)
    assert s.sender.sent[-1] == (CUSTOMER, "Your booking CS-0001 has been created.")


def test_an_image_is_downloaded_and_given_to_the_agent_as_an_attachment(make):
    s = make(client=PretendOpenAI(answer(text("Thanks, which booking is this for?"))))
    s.receive(s.dm(attachments=image()))
    s.worker.run_once()
    assert s.download.urls == ["https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=1"]
    assert s.client.requests[0]["input"][-1] == {"role": "user", "content": "[Customer attached image #1]"}
    [attachment] = s.db.execute("SELECT * FROM conversation_attachments").fetchall()
    assert attachment["number"] == 1
    assert (s.path.parent / "attachments" / str(attachment["conversation_id"]) / attachment["file_name"]) \
        .read_bytes() == JPG


def test_the_arabizi_guard_still_protects_arabizi_customers(make):
    s = make(client=PretendOpenAI(answer(text("هلا! شلون أقدر أساعدك؟")), answer(text("Hala! shlon agdar asa3dk?"))))
    s.receive(s.dm("shlonkum, 3indkum setup?"))
    s.worker.run_once()
    assert s.sender.sent[-1] == (CUSTOMER, "Hala! shlon agdar asa3dk?")
    assert s.conversation().language.value == "arabizi"


def test_when_the_ai_has_a_problem_the_owner_gets_a_handoff_and_the_customer_the_handoff_reply(make):
    error = openai.APIConnectionError(request=httpx2.Request("POST", "https://api.openai.com/v1/responses"))
    s = make(client=PretendOpenAI(error))
    s.receive(s.dm("hello"))
    s.worker.run_once()
    assert s.sender.sent[-1] == (CUSTOMER, HANDOFF)
    [handoff] = s.service.list_handoffs()
    assert (handoff.channel, handoff.channel_user_id) == ("instagram", CUSTOMER)
    assert s.inbound()[0]["status"] == "done"


# --- Unsupported messages (decision D) ----------------------------------------------------------------

@pytest.mark.parametrize("attachment", [
    voice_note(),
    [{"type": "video", "payload": {"url": "https://lookaside.fbsbx.com/v"}}],
    [{"type": "like_heart"}],
    [{"type": "image", "payload": {"url": "https://lookaside.fbsbx.com/s", "sticker_id": 369239263222822}}],
    [{"type": "ig_reel", "payload": {"reel_video_id": "1"}}],
    [{"type": "share", "payload": {"url": "https://www.instagram.com/p/x"}}],
    [{"type": "story_mention", "payload": {"url": "https://lookaside.fbsbx.com/s"}}],
], ids=["voice note", "video", "heart", "sticker", "reel", "shared post", "story mention"])
def test_unsupported_messages_get_the_clear_reply_without_asking_the_ai(make, attachment):
    s = make()
    s.receive(s.dm(attachments=attachment))
    s.worker.run_once()
    assert s.client.requests == []
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, UNSUPPORTED]
    assert s.download.urls == []
    assert s.history()[-1] == {"role": "assistant", "content": UNSUPPORTED}


def test_text_with_a_voice_note_gets_the_ai_reply_and_the_unsupported_reply(make):
    s = make(client=PretendOpenAI(answer(text("It's 50 KWD."))))
    s.receive(s.dm("How much?"), s.dm(attachments=voice_note()))
    s.worker.run_once()
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, "It's 50 KWD.", UNSUPPORTED]


# --- Echoes and the owner taking over (decision C) -----------------------------------------------

def test_the_echo_of_our_own_reply_changes_nothing(make):
    s = make(client=PretendOpenAI(answer(text("Hello!"))))
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    ours = s.replies()[-1]["external_id"]
    s.receive(s.dm("Hello!", echo=True, mid=ours))
    [result] = s.worker.run_once()
    assert result.what == "echo"
    assert s.inbound()[-1]["status"] == "ignored"
    assert not s.conversation().ai_paused


def test_the_owner_replying_in_instagram_pauses_the_ai(make):
    s = make(client=PretendOpenAI(answer(text("Hello!"))))
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    s.receive(s.dm("I'll handle this personally", echo=True))
    s.worker.run_once()
    stored = s.conversation()
    assert stored.ai_paused and stored.pause_reason == "The owner replied in Instagram"
    assert s.history()[-1] == {"role": "assistant",
                               "content": "(The owner replied personally: I'll handle this personally)"}

    # While paused: the customer's messages are kept for later, but not answered.
    s.receive(s.dm("Thanks!"), s.dm(attachments=image()))
    [result] = s.worker.run_once()
    assert result.what == "recorded"
    assert len(s.client.requests) == 1 and len(s.sender.sent) == 2
    assert s.history()[-1] == {"role": "user", "content": "Thanks!\n[Customer sent an image]"}
    assert {m["status"] for m in s.inbound()} == {"done"}


def test_after_resuming_the_ai_answers_again_without_a_second_disclosure(make, capsys):
    s = make(client=PretendOpenAI(answer(text("Hello!")), answer(text("You're welcome!"))))
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    s.receive(s.dm("Owner here", echo=True))
    s.worker.run_once()
    assert main(["resume", CUSTOMER, "--db", str(s.path)], clock=s.clock) == 0
    assert "answers 1234567890123456 again" in capsys.readouterr().out
    s.receive(s.dm("Thank you"))
    s.worker.run_once()
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, "Hello!", "You're welcome!"]


def test_replies_not_yet_sent_are_cancelled_when_the_owner_takes_over(make):
    busy = SendError("Instagram busy")
    s = make(client=PretendOpenAI(answer(text("Hello!"))), sender=PretendSender(busy))
    s.receive(s.dm("Hi"))
    s.worker.run_once()                                   # the disclosure waits for a retry
    s.receive(s.dm("I'll take it from here", echo=True))
    s.clock.advance(minutes=2)
    s.worker.run_once()
    assert s.sender.sent == []
    assert {r["status"] for r in s.replies()} == {"cancelled"}


def test_an_owner_message_before_any_ai_reply_pauses_a_new_conversation(make):
    s = make()
    s.receive(s.dm("Hi, welcome!", echo=True))
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    assert s.conversation().ai_paused and not s.conversation().disclosure_sent
    assert s.client.requests == [] and s.sender.sent == []


# --- Old messages ------------------------------------------------------------------------------------

def test_messages_older_than_24_hours_are_ignored(make):
    s = make()
    s.receive(s.dm("Hi, anyone there?", at=NOW - timedelta(hours=25)))
    [result] = s.worker.run_once()
    assert result.what == "ignored"
    assert s.inbound()[0]["status"] == "ignored" and "older than 24 hours" in s.inbound()[0]["error"]
    assert s.client.requests == [] and s.sender.sent == []


def test_an_old_owner_echo_still_pauses_the_ai(make):
    s = make()
    s.receive(s.dm("Owner reply", echo=True, at=NOW - timedelta(hours=30)))
    s.worker.run_once()
    assert s.conversation().ai_paused


# --- Done only after success; retries ------------------------------------------------------------

def test_a_failed_download_is_retried_later_and_nothing_is_half_saved(make):
    s = make(client=PretendOpenAI(answer(text("Thanks!"))), download=Downloads(DownloadError("timed out"), JPG))
    s.receive(s.dm(attachments=image()))
    [result] = s.worker.run_once()
    assert result.what == "retry"
    [message] = s.inbound()
    assert (message["status"], message["attempts"], message["error"]) == ("waiting", 1, "DownloadError: timed out")
    assert s.replies() == [] and s.sender.sent == []

    s.worker.run_once()                                   # too early for the retry
    assert s.inbound()[0]["attempts"] == 1
    s.clock.advance(minutes=1)
    s.worker.run_once()
    assert s.inbound()[0]["status"] == "done"
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, "Thanks!"]


def test_after_three_failed_attempts_the_owner_gets_a_handoff(make):
    failure = DownloadError("timed out")
    s = make(download=Downloads(failure, failure, failure))
    s.receive(s.dm("here is the transfer", attachments=image()))
    for wait in (0, 1, 5):
        s.clock.advance(minutes=wait)
        s.worker.run_once()
    [message] = s.inbound()
    assert (message["status"], message["attempts"]) == ("failed", 3)
    [handoff] = s.service.list_handoffs()
    assert handoff.type is HandoffType.OTHER and handoff.channel_user_id == CUSTOMER
    assert "'here is the transfer'" in handoff.summary and "reply to them yourself" in handoff.summary
    assert s.client.requests == [] and s.sender.sent == []


def test_a_message_is_done_only_when_its_replies_are_saved(make, monkeypatch):
    s = make(client=PretendOpenAI(answer(text("Hello!"))))
    s.receive(s.dm("Hi"))

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(instagram_worker, "split_text", broken)   # fails inside the saving transaction
    [result] = s.worker.run_once()
    assert result.what == "retry"
    assert s.inbound()[0]["status"] == "waiting"
    assert s.replies() == [] and s.history() == []               # the conversation wasn't saved either
    assert not s.conversation().disclosure_sent


def test_one_customers_problem_does_not_hold_up_another(make):
    s = make(client=PretendOpenAI(answer(text("Hello other!"))), download=Downloads(DownloadError("timed out")))
    s.receive(s.dm(attachments=image()), s.dm("Hi", customer=OTHER_CUSTOMER))
    s.worker.run_once()
    assert (OTHER_CUSTOMER, "Hello other!") in s.sender.sent
    assert s.inbound()[0]["status"] == "waiting"


def test_two_workers_never_take_the_same_message(make):
    s = make()
    s.receive(s.dm("Hi"))
    [row] = s.worker._waiting_rows()
    incoming = read_incoming(row, INFO.timezone)
    s.worker._claim([incoming])
    with pytest.raises(_AlreadyClaimed):
        s.worker._claim([incoming])


# --- Sending --------------------------------------------------------------------------------------------

def test_a_temporary_send_failure_is_retried_in_order(make):
    s = make(client=PretendOpenAI(answer(text("Hello!"))), sender=PretendSender(SendError("busy")))
    s.receive(s.dm("Hi"))
    results = s.worker.run_once()
    assert [r.what for r in results] == ["answered", "retry"]
    assert s.sender.sent == []                            # the reply never overtakes the disclosure
    s.clock.advance(minutes=1)
    s.worker.run_once()
    assert [t for _, t in s.sender.sent] == [DISCLOSURE, "Hello!"]


def test_a_permanent_send_failure_hands_the_replies_to_the_owner(make):
    s = make(client=PretendOpenAI(answer(text("Hello!"))),
             sender=PretendSender(SendError("No matching user", permanent=True)))
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    assert {r["status"] for r in s.replies()} == {"failed"}
    [handoff] = s.service.list_handoffs()
    assert DISCLOSURE in handoff.summary and "Hello!" in handoff.summary
    assert "No matching user" in handoff.summary


def test_outside_the_24_hour_window_the_reply_becomes_send_yourself(make):
    s = make(client=PretendOpenAI(answer(text("Hello!"))), sender=PretendSender(SendError("busy")))
    s.receive(s.dm("Hi"))
    s.worker.run_once()
    s.clock.advance(hours=24)
    [result] = s.worker.run_once()
    assert result.what == "send_yourself"
    assert s.sender.sent == []
    assert {r["status"] for r in s.replies()} == {"send_yourself"}
    [handoff] = s.service.list_handoffs()
    assert "24-hour" in handoff.summary and "Hello!" in handoff.summary


def test_a_long_arabic_reply_is_split_into_instagram_sized_messages(make):
    long_reply = "\n".join(f"السطر رقم {n}: تفاصيل الحجز والموقع والتاريخ والمبلغ المطلوب" for n in range(40))
    s = make(client=PretendOpenAI(answer(text(long_reply))))
    s.receive(s.dm("مرحبا"))
    s.worker.run_once()
    parts = [t for _, t in s.sender.sent][1:]
    assert len(parts) > 1
    assert all(len(part.encode("utf-8")) <= MAX_TEXT_BYTES for part in parts)
    assert "\n".join(parts) == long_reply


# --- Pieces -------------------------------------------------------------------------------------------------

def test_split_text():
    assert split_text("short") == ["short"]
    assert split_text("  ") == []
    assert split_text("aaaa bbbb", limit=5) == ["aaaa", "bbbb"]
    assert split_text("para one\n\npara two", limit=10) == ["para one", "para two"]
    assert split_text("abcdefghij", limit=4) == ["abcd", "efgh", "ij"]
    assert all(len(p.encode()) <= 7 for p in split_text("ححححححححححح", limit=7))


def test_instagram_sender_sends_the_right_request_and_returns_the_message_id():
    calls = []

    def post(path, body, token):
        calls.append((path, body, token))
        return {"recipient_id": CUSTOMER, "message_id": "mid.sent"}

    assert InstagramSender("TOKEN", post=post).send(CUSTOMER, "Hello") == "mid.sent"
    assert calls == [("me/messages", {"recipient": {"id": CUSTOMER}, "message": {"text": "Hello"}}, "TOKEN")]


@pytest.mark.parametrize(("error", "permanent"), [
    (GraphError("outside the window", 400, 10), True),
    (GraphError("no such user", 400, 100), True),
    (GraphError("token expired", 401, 190), True),
    (GraphError("rate limited", 400, 4), False),
    (GraphError("server error", 500, 2), False),
    (GraphError("no internet"), False),
])
def test_instagram_sender_says_which_errors_are_worth_retrying(error, permanent):
    def post(*args):
        raise error
    with pytest.raises(SendError) as raised:
        InstagramSender("TOKEN", post=post).send(CUSTOMER, "Hello")
    assert raised.value.permanent is permanent


def test_the_token_goes_in_a_header_and_never_into_errors(monkeypatch):
    seen = []

    def refuse(request, timeout):
        seen.append(request)
        body = json.dumps({"error": {"message": "Invalid token SECRET-TOKEN", "code": 190}}).encode()
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, __import__("io").BytesIO(body))

    monkeypatch.setattr(senders.urllib.request, "urlopen", refuse)
    with pytest.raises(SendError) as raised:
        InstagramSender("SECRET-TOKEN").send(CUSTOMER, "Hello")
    assert "SECRET-TOKEN" not in str(raised.value) and raised.value.permanent
    [request] = seen
    assert "SECRET-TOKEN" not in request.full_url
    assert request.get_header("Authorization") == "Bearer SECRET-TOKEN"
    assert json.loads(request.data) == {"recipient": {"id": CUSTOMER}, "message": {"text": "Hello"}}


def test_downloads_only_accept_https_and_limit_the_size(monkeypatch):
    with pytest.raises(DownloadError, match="https"):
        download_image("http://example.com/x.jpg")

    class Big:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return b"x" * limit

    monkeypatch.setattr(instagram_worker.urllib.request, "urlopen", lambda request, timeout: Big())
    with pytest.raises(DownloadError, match="larger than 10 MB"):
        download_image("https://lookaside.fbsbx.com/x")
    assert MAX_PROOF_BYTES == 10 * 1024 * 1024


def test_the_new_replies_are_owner_texts_in_business_toml():
    assert DISCLOSURE.startswith("Hi! You're chatting with CozySetup's automated assistant.")
    assert "voice notes" in UNSUPPORTED


# --- Database version 4 ---------------------------------------------------------------------------------

def test_a_version_3_database_is_upgraded_keeping_its_messages(tmp_path):
    path = tmp_path / "cozysetup.db"
    db = sqlite3.connect(path)
    db.executescript(f"BEGIN; {SCHEMA_V1} PRAGMA user_version = 1; COMMIT;")
    for version in (2, 3):
        db.executescript(f"BEGIN; {MIGRATIONS[version]} PRAGMA user_version = {version}; COMMIT;")
    db.execute("INSERT INTO inbound_messages (channel, external_id, channel_user_id, received_at, payload) "
               "VALUES ('instagram', 'mid.1', ?, ?, '{}')", (CUSTOMER, NOW.isoformat()))
    db.commit()
    db.close()

    db = connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    row = db.execute("SELECT * FROM inbound_messages").fetchone()
    assert (row["external_id"], row["status"], row["attempts"], row["last_attempt_at"]) == \
        ("mid.1", "waiting", 0, None)
    assert db.execute("SELECT COUNT(*) FROM conversation_replies").fetchone()[0] == 0
    db.close()
    backup = sqlite3.connect(tmp_path / f"cozysetup.db.before-v{SCHEMA_VERSION}.bak")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
    backup.close()


def test_replies_refuse_unknown_kinds_and_statuses(tmp_path):
    db = connect(tmp_path / "x.db")
    db.execute("INSERT INTO conversations (channel, channel_user_id, created_at, updated_at) "
               "VALUES ('instagram', ?, 'x', 'x')", (CUSTOMER,))
    for kind, status in (("poem", "pending"), ("reply", "maybe")):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO conversation_replies (conversation_id, kind, text, status, created_at, "
                       "updated_at) VALUES (1, ?, 'x', ?, 'x', 'x')", (kind, status))
    db.close()


# --- The command ----------------------------------------------------------------------------------------

def test_work_once_answers_and_sends(make, capsys):
    s = make()
    s.receive(s.dm("Hi"))
    client, sender = PretendOpenAI(answer(text("Hello!"))), PretendSender()
    assert main(["work", "--once", "--db", str(s.path)], client=client, sender=sender, clock=s.clock) == 0
    out = capsys.readouterr().out
    assert "answered" in out and "sent" in out
    assert [t for _, t in sender.sent] == [DISCLOSURE, "Hello!"]


def test_work_keeps_going_after_a_failed_round(make, capsys, monkeypatch):
    s = make()
    monkeypatch.setattr(InstagramWorker, "run_once", lambda self: 1 / 0)
    assert main(["work", "--db", str(s.path)], client=PretendOpenAI(), sender=PretendSender(), clock=s.clock,
                sleep=lambda seconds: None, max_rounds=2) == 0
    assert capsys.readouterr().err.count("this round failed") == 2


def test_conversations_lists_paused_ones(make, capsys):
    s = make()
    s.receive(s.dm("Owner here", echo=True))
    s.worker.run_once()
    assert main(["conversations", "--db", str(s.path)]) == 0
    assert "AI paused" in capsys.readouterr().out


def test_resume_an_unknown_conversation(make, capsys):
    s = make()
    assert main(["resume", "999", "--db", str(s.path)]) == 1
    assert "No Instagram conversation" in capsys.readouterr().err


def test_a_bug_in_the_sender_is_treated_as_temporary(make):
    s = make(client=PretendOpenAI(answer(text("Hello!"))), sender=PretendSender(RuntimeError("oops")))
    s.receive(s.dm("Hi"))
    results = s.worker.run_once()
    assert results[-1].what == "retry" and "RuntimeError: oops" in results[-1].detail
    assert s.replies()[0]["status"] == "pending"
