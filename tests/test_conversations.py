"""Tests for database version 3 and the conversation store (Step 7.2) - no API calls."""

import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
    ResponseReasoningItem,
)

from cozysetup.agent import ARABIZI_REWRITE_INSTRUCTIONS
from cozysetup.bookings import BookingService
from cozysetup.business_info import load_business_info
from cozysetup.conversations import ConversationStore, history_to_json, preview_from_json, preview_to_json
from cozysetup.database import MIGRATIONS, SCHEMA_V1, SCHEMA_VERSION, BookingStatus, Language, connect
from tests.test_agent import PretendOpenAI, answer, call, text

INFO = load_business_info()
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=INFO.timezone)
IGSID = "1234567890123456"
BOOKING = {"date": "2026-10-01", "location_id": "julaia", "customer_name": "Ahmad",
           "customer_phone": "99999999", "payment_choice": "deposit"}
JPG = b"\xff\xd8\xff\xe0" + bytes(100)


class Server:
    """One run of the server: its own database connection, like a real restart."""

    def __init__(self, path, attachments_dir):
        self.db = connect(path)
        self.service = BookingService(self.db, INFO, clock=lambda: NOW, proofs_dir=path.parent / "proofs")
        self.store = ConversationStore(self.service, attachments_dir)

    def close(self):
        self.db.close()


@pytest.fixture
def paths(tmp_path):
    return tmp_path / "cozysetup.db", tmp_path / "conversation_attachments"


@pytest.fixture
def server(paths):
    run = Server(*paths)
    yield run
    run.close()


def restart(paths):
    return Server(*paths)


# --- Database version 3 -----------------------------------------------------------------

def make_version_2_file(path):
    db = sqlite3.connect(path)
    db.executescript(f"BEGIN; {SCHEMA_V1} PRAGMA user_version = 1; COMMIT;")
    db.executescript(f"BEGIN; {MIGRATIONS[2]} PRAGMA user_version = 2; COMMIT;")
    db.execute("INSERT INTO handoffs (created_at, type, summary) VALUES (?, 'weather', 'windy?')", (NOW.isoformat(),))
    db.commit()
    db.close()


def test_a_version_2_database_is_upgraded_to_3_keeping_its_data(tmp_path):
    path = tmp_path / "cozysetup.db"
    make_version_2_file(path)
    db = connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    assert db.execute("SELECT summary FROM handoffs").fetchone()[0] == "windy?"
    for table in ("conversations", "conversation_attachments", "inbound_messages"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    db.close()
    backup = sqlite3.connect(tmp_path / "cozysetup.db.before-v3.bak")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == 2
    backup.close()


def insert_conversation(db, **changes):
    row = {"channel": "instagram", "channel_user_id": IGSID, "created_at": "x", "updated_at": "x", **changes}
    names = ", ".join(row)
    with db:
        return db.execute(f"INSERT INTO conversations ({names}) VALUES ({', '.join('?' * len(row))})",
                          tuple(row.values())).lastrowid


def test_one_conversation_per_customer_per_channel(server):
    insert_conversation(server.db)
    insert_conversation(server.db, channel="whatsapp")          # same id, other channel: fine
    with pytest.raises(sqlite3.IntegrityError):
        insert_conversation(server.db)


@pytest.mark.parametrize("changes", [{"language": "french"}, {"ai_paused": 2}, {"disclosure_sent": 5},
                                     {"channel_user_id": None}])
def test_conversation_refuses_bad_values(server, changes):
    with pytest.raises(sqlite3.IntegrityError):
        insert_conversation(server.db, **changes)


def test_attachments_belong_to_a_real_conversation_and_are_numbered_once(server):
    conversation_id = insert_conversation(server.db)
    insert = "INSERT INTO conversation_attachments (conversation_id, number, file_name, created_at) VALUES (?, ?, ?, 'x')"
    with server.db:
        server.db.execute(insert, (conversation_id, 1, "1.jpg"))
    for values in [(conversation_id, 1, "again.jpg"), (999, 1, "x.jpg"), (conversation_id, 0, "zero.jpg")]:
        with pytest.raises(sqlite3.IntegrityError):
            with server.db:
                server.db.execute(insert, values)


def test_inbound_messages_are_recorded_once_with_a_known_status(server):
    insert = ("INSERT INTO inbound_messages (channel, external_id, channel_user_id, received_at, payload, status) "
              "VALUES ('instagram', ?, ?, 'x', '{}', ?)")
    with server.db:
        server.db.execute(insert, ("mid.1", IGSID, "waiting"))
        server.db.execute(insert, ("mid.2", IGSID, "done"))
    for values in [("mid.1", IGSID, "waiting"), ("mid.3", IGSID, "maybe")]:
        with pytest.raises(sqlite3.IntegrityError):
            with server.db:
                server.db.execute(insert, values)


# --- Finding a conversation ----------------------------------------------------------------

def test_get_or_create_makes_exactly_one_conversation(server):
    first = server.store.get_or_create("instagram", IGSID)
    again = server.store.get_or_create("instagram", IGSID)
    assert first.id == again.id
    assert (first.channel, first.channel_user_id, first.created_at) == ("instagram", IGSID, NOW)
    assert (first.language, first.ai_paused, first.disclosure_sent, first.last_customer_message_at) == (
        None, False, False, None)
    assert server.store.get_or_create("instagram", "another-customer").id != first.id
    assert server.store.get("instagram", "nobody") is None


def test_a_new_conversation_starts_empty(server):
    agent = server.store.open_agent(PretendOpenAI(), server.store.get_or_create("instagram", IGSID))
    assert agent.messages == [] and agent.customer_language is None
    assert agent.conversation.last_preview is None and agent.conversation.attachments == {}
    assert (agent.conversation.channel, agent.conversation.channel_user_id) == ("instagram", IGSID)


# --- Continuing a conversation after a restart --------------------------------------------------

def test_the_summary_in_one_run_and_the_yes_in_the_next(paths):
    # Run 1: the customer gives the details; the AI shows the summary. Then the server restarts.
    first = restart(paths)
    stored = first.store.get_or_create("instagram", IGSID)
    agent = first.store.open_agent(
        PretendOpenAI(answer(call("show_booking_summary", **BOOKING)), answer(text("<summary>"))), stored)
    agent.reply("Ahmad, 99999999, deposit")
    first.store.save_agent(stored, agent)
    first.close()

    # Run 2: the customer says yes. create_booking only works if the summary was kept.
    second = restart(paths)
    model = PretendOpenAI(answer(call("create_booking", **BOOKING, customer_language="en")), answer(text("<pay>")))
    agent = second.store.open_agent(model, second.store.get("instagram", IGSID))
    agent.reply("yes")
    booking = second.service.get_booking("CS-0001")
    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert (booking.channel, booking.channel_user_id) == ("instagram", IGSID)
    sent = model.requests[0]["input"]
    assert sent[0] == {"role": "user", "content": "Ahmad, 99999999, deposit"}    # the earlier turn was replayed
    assert sent[-1] == {"role": "user", "content": "yes"}
    second.close()


def test_without_saving_the_summary_is_lost_and_booking_is_refused(paths):
    first = restart(paths)
    stored = first.store.get_or_create("instagram", IGSID)
    first.store.open_agent(PretendOpenAI(answer(call("show_booking_summary", **BOOKING)), answer(text("s"))),
                           stored).reply("details")                   # not saved
    first.close()
    second = restart(paths)
    model = PretendOpenAI(answer(call("create_booking", **BOOKING)), answer(text("?")))
    second.store.open_agent(model, second.store.get("instagram", IGSID)).reply("yes")
    assert second.service.list_bookings() == []
    second.close()


def test_a_screenshot_sent_in_one_run_is_attached_in_the_next(paths):
    first = restart(paths)
    stored = first.store.get_or_create("instagram", IGSID)
    first.service.create_booking(booking_date=datetime(2026, 10, 1).date(), location_id="julaia",
                                 customer_name="Ahmad", customer_phone="99999999", payment_choice="deposit",
                                 channel="instagram", channel_user_id=IGSID)
    agent = first.store.open_agent(PretendOpenAI(answer(text("Which booking is it for?"))), stored)
    agent.reply("here is my transfer", images=[JPG])
    first.store.save_agent(stored, agent)
    first.close()
    assert (paths[1] / str(stored.id) / "1.jpg").read_bytes() == JPG     # kept as a file

    second = restart(paths)
    model = PretendOpenAI(
        answer(call("attach_payment_proof", reference="CS-0001", customer_phone="99999999", attachment_number=1)),
        answer(text("Thank you, we've received your screenshot.")))
    agent = second.store.open_agent(model, second.store.get("instagram", IGSID))
    agent.reply("CS-0001, 99999999")
    assert second.service.get_booking("CS-0001").status is BookingStatus.PAYMENT_SUBMITTED
    second.close()


def test_image_numbers_continue_after_a_restart_and_are_saved_once(paths):
    first = restart(paths)
    stored = first.store.get_or_create("instagram", IGSID)
    agent = first.store.open_agent(PretendOpenAI(answer(text("ok"))), stored)
    agent.reply("", images=[JPG])
    first.store.save_agent(stored, agent)
    first.store.save_agent(stored, agent)                             # saving twice: still one row
    first.close()

    second = restart(paths)
    model = PretendOpenAI(answer(text("ok")))
    agent = second.store.open_agent(model, second.store.get("instagram", IGSID))
    agent.reply("another", images=[b"\x89PNG\r\n\x1a\n" + bytes(50)])
    assert model.requests[0]["input"][-1]["content"] == "another\n[Customer attached image #2]"
    second.store.save_agent(stored, agent)
    rows = second.db.execute("SELECT number, file_name FROM conversation_attachments ORDER BY number").fetchall()
    assert [tuple(r) for r in rows] == [(1, "1.jpg"), (2, "2.png")]
    second.close()


def test_the_arabizi_language_is_kept_so_the_guard_still_works(paths):
    first = restart(paths)
    stored = first.store.get_or_create("instagram", IGSID)
    agent = first.store.open_agent(PretendOpenAI(answer(text("Hala!"))), stored)
    agent.reply("hala, abi a7jiz setup")
    assert first.store.save_agent(stored, agent).language is Language.ARABIZI
    first.close()

    second = restart(paths)
    model = PretendOpenAI(answer(text("Dizz li raqam تلفونك")), answer(text("Dizz li raqam tilifoonik")))
    agent = second.store.open_agent(model, second.store.get("instagram", IGSID))
    assert agent.customer_language is Language.ARABIZI
    assert agent.reply("b Julaia yom el khamees 1 October").text == "Dizz li raqam tilifoonik"
    assert [r for r in model.requests if r.get("instructions") == ARABIZI_REWRITE_INSTRUCTIONS]
    second.close()


# --- The history is stored as plain JSON that OpenAI accepts back --------------------------------

def test_real_openai_items_are_stored_as_plain_data():
    items = [
        {"role": "user", "content": "hi"},
        ResponseReasoningItem(id="rs_1", type="reasoning", summary=[], encrypted_content="ENCRYPTED"),
        ResponseFunctionToolCall(type="function_call", call_id="call_1", name="check_availability",
                                 arguments='{"date": "2026-10-01"}'),
        {"type": "function_call_output", "call_id": "call_1", "output": "{}"},
        ResponseOutputMessage(id="msg_1", type="message", role="assistant", status="completed",
                              content=[ResponseOutputText(type="output_text", text="هلا", annotations=[])]),
    ]
    stored = json.loads(history_to_json(items))
    assert stored[0] == {"role": "user", "content": "hi"}
    assert stored[1]["type"] == "reasoning" and stored[1]["encrypted_content"] == "ENCRYPTED"
    assert stored[2] == {"type": "function_call", "call_id": "call_1", "name": "check_availability",
                         "arguments": '{"date": "2026-10-01"}'}
    assert stored[4]["content"][0]["text"] == "هلا"          # Arabic kept as-is
    assert "هلا" in history_to_json(items)                    # not escaped


def test_booking_summary_round_trip(server):
    preview = server.service.preview_booking(booking_date=datetime(2026, 10, 1).date(), location_id="julaia",
                                             customer_name="أحمد", customer_phone="99999999",
                                             payment_choice="deposit")
    assert preview_from_json(preview_to_json(preview)) == preview
    assert preview_to_json(None) is None and preview_from_json(None) is None


# --- Window, disclosure and pause --------------------------------------------------------------

def test_the_time_of_the_customers_last_message_is_kept(server):
    stored = server.store.get_or_create("instagram", IGSID)
    later = NOW + timedelta(hours=3)
    assert server.store.mark_customer_message(stored, later).last_customer_message_at == later


def test_the_disclosure_is_remembered(server):
    stored = server.store.get_or_create("instagram", IGSID)
    assert server.store.mark_disclosure_sent(stored).disclosure_sent is True


def test_pausing_and_resuming_the_ai(server):
    stored = server.store.get_or_create("instagram", IGSID)
    paused = server.store.pause_ai(stored, "the owner replied in the Instagram app")
    assert (paused.ai_paused, paused.paused_at, paused.pause_reason) == (
        True, NOW, "the owner replied in the Instagram app")
    resumed = server.store.resume_ai(paused)
    assert (resumed.ai_paused, resumed.paused_at, resumed.pause_reason) == (False, None, None)


def test_saving_keeps_the_other_facts(server):
    stored = server.store.pause_ai(server.store.mark_disclosure_sent(server.store.get_or_create("instagram", IGSID)),
                                   "owner")
    agent = server.store.open_agent(PretendOpenAI(answer(text("hi"))), stored)
    agent.reply("hello")
    saved = server.store.save_agent(stored, agent)
    assert (saved.ai_paused, saved.disclosure_sent) == (True, True)
