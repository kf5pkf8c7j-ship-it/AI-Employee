"""Tests for the terminal chat, with a pretend model and pretend typing - no cost."""

from datetime import date, datetime

from cozysetup import chat, outbox
from cozysetup.bookings import BookingService
from cozysetup.database import connect
from cozysetup.tools import Conversation
from cozysetup.business_info import load_business_info
from cozysetup.settings import MissingApiKey
from tests.test_agent import PretendOpenAI, answer, call, text

INFO = load_business_info()
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=INFO.timezone)


def typing(*lines):
    """Pretend the customer types these lines, then closes the chat (Ctrl+D)."""
    remaining = list(lines)

    def ask(prompt):
        if not remaining:
            raise EOFError
        return remaining.pop(0)
    return ask


def run(tmp_path, model, *lines):
    return chat.main(["--db", str(tmp_path / "practice.db")], client=model, ask=typing(*lines), clock=lambda: NOW)


def test_a_conversation_shows_replies_and_cost(tmp_path, capsys):
    model = PretendOpenAI(
        answer(call("check_availability", date="2026-10-01")),
        answer(text("Thursday is available!")),
    )
    assert run(tmp_path, model, "Is Thursday free?", "/quit") == 0
    out = capsys.readouterr().out
    assert "CozySetup: Thursday is available!" in out
    assert "2 request(s)" in out and "tools: check_availability" in out and "about $" in out
    assert "Total this session: 2 requests" in out


def test_the_conversation_is_logged_next_to_the_database(tmp_path):
    run(tmp_path, PretendOpenAI(answer(text("Hi!"))), "hello")
    [log] = (tmp_path / "conversations").glob("*_terminal.jsonl")
    assert '"event": "customer"' in log.read_text()


def test_empty_lines_are_ignored(tmp_path):
    model = PretendOpenAI(answer(text("Hi!")))
    run(tmp_path, model, "", "   ", "hello")
    assert len(model.requests) == 1


def test_image_is_sent_as_an_attachment(tmp_path, capsys):
    screenshot = tmp_path / "transfer.jpg"
    screenshot.write_bytes(b"\xff\xd8\xff" + bytes(50))
    model = PretendOpenAI(answer(text("Thanks!")))
    run(tmp_path, model, f"/image {screenshot}")
    assert model.requests[0]["input"][0]["content"] == "[Customer attached image #1]"


def test_image_that_does_not_exist(tmp_path, capsys):
    model = PretendOpenAI()
    run(tmp_path, model, "/image /no/such/file.jpg")
    assert "no such file" in capsys.readouterr().out
    assert model.requests == []


def test_new_starts_a_fresh_conversation(tmp_path):
    model = PretendOpenAI(answer(text("Hi Ahmad")), answer(text("Hi Sara")))
    run(tmp_path, model, "I'm Ahmad", "/new", "I'm Sara")
    assert model.requests[1]["input"] == [{"role": "user", "content": "I'm Sara"}]
    assert len(list((tmp_path / "conversations").glob("*.jsonl"))) >= 1


def test_ctrl_d_leaves_cleanly(tmp_path):
    assert run(tmp_path, PretendOpenAI()) == 0


def test_missing_key_is_explained(tmp_path, monkeypatch, capsys):
    def no_key():
        raise MissingApiKey("No OpenAI API key found.")
    monkeypatch.setattr(chat, "load_api_key", no_key)
    assert chat.main(["--db", str(tmp_path / "p.db")], ask=typing()) == 1
    assert "No OpenAI API key found" in capsys.readouterr().err


def test_default_is_the_practice_database():
    assert chat.PRACTICE_DB_PATH.parts[-2:] == ("practice", "practice.db")


# --- Messages delivered to the practice customer (Step 6.5) ---------------------------------


BOOKING = {"date": "2026-10-01", "location_id": "julaia", "customer_name": "Ahmad",
           "customer_phone": "99999999", "payment_choice": "deposit"}


class Delivering:
    def __init__(self):
        self.sent = []

    def send(self, recipient, text):
        self.sent.append(recipient)


def owner_and_sender(db_path, actions):
    """Pretend that, between two customer messages, the owner and the outbox sender do something."""
    db = connect(db_path)
    service = BookingService(db, INFO, clock=lambda: NOW, proofs_dir=db_path.parent / "payment_proofs")
    for action in actions:
        action(service)
    db.close()


def approve_and_deliver(service):
    service.approve_payment("CS-0001")
    outbox.deliver_due(service, {"terminal": Delivering()})


def test_a_delivered_confirmation_appears_after_the_next_reply_only_once(tmp_path, capsys):
    db_path = tmp_path / "practice.db"
    model = PretendOpenAI(
        answer(call("show_booking_summary", **BOOKING)), answer(text("<summary>")),
        answer(call("create_booking", **BOOKING, customer_language="en")), answer(text("<payment instructions>")),
        answer(text("You're welcome!")),
        answer(text("Anything else?")),
    )
    lines = iter(["Ahmad, 99999999, deposit", "yes", "thanks", "ok"])

    def ask(prompt):
        line = next(lines, None)
        if line is None:
            raise EOFError
        if line == "thanks":                        # the owner approves; the outbox sender delivers
            owner_and_sender(db_path, [approve_and_deliver])
        return line

    chat.main(["--db", str(db_path)], client=model, ask=ask, clock=lambda: NOW)
    out = capsys.readouterr().out
    assert out.count("📩 Message from CozySetup:") == 1
    assert "   Your booking CS-0001 is confirmed!" in out
    assert out.index("You're welcome!") < out.index("📩 Message from CozySetup:")   # after that reply


def test_inbox_command_shows_new_messages_or_says_there_are_none(tmp_path, capsys):
    db_path = tmp_path / "practice.db"
    model = PretendOpenAI(
        answer(call("show_booking_summary", **BOOKING)), answer(text("<summary>")),
        answer(call("create_booking", **BOOKING)), answer(text("<pay>")),
    )
    lines = iter(["details", "yes", "/inbox", "DELIVER", "/inbox", "/inbox"])

    def ask(prompt):
        line = next(lines, None)
        if line is None:
            raise EOFError
        if line == "DELIVER":
            owner_and_sender(db_path, [approve_and_deliver])
            return "/inbox"
        return line

    chat.main(["--db", str(db_path)], client=model, ask=ask, clock=lambda: NOW)
    out = capsys.readouterr().out
    assert out.count("(no new messages)") == 3           # before delivery, and after it was shown
    assert out.count("📩 Message from CozySetup:") == 1


def test_the_start_explains_how_messages_arrive(tmp_path, capsys):
    run(tmp_path, PretendOpenAI())
    out = capsys.readouterr().out
    assert "/inbox        show messages CozySetup has sent you" in out
    assert f"uv run cozysetup-outbox --db {tmp_path / 'practice.db'} watch" in out


def inbox_setup(tmp_path):
    db = connect(tmp_path / "p.db")
    service = BookingService(db, INFO, clock=lambda: NOW, proofs_dir=tmp_path / "proofs")
    return db, service


def make_booking(service, recipient, booking_date=date(2026, 10, 1), channel="terminal"):
    booking = service.create_booking(
        booking_date=booking_date, location_id="julaia", customer_name="X", customer_phone="99999999",
        payment_choice="full", channel=channel, channel_user_id=recipient,
    )
    service.approve_payment(booking.reference)
    return booking


def test_inbox_shows_only_this_conversations_delivered_messages(tmp_path):
    db, service = inbox_setup(tmp_path)
    make_booking(service, "me")
    make_booking(service, "someone-else", booking_date=date(2026, 10, 2))
    inbox = chat.Inbox(db, Conversation("terminal", "me"))
    assert inbox.new_messages() == []                    # queued, but not delivered yet

    outbox.deliver_due(service, {"terminal": Delivering()})
    delivered = inbox.new_messages()
    assert [m.text.splitlines()[0] for m in delivered] == ["Your booking CS-0001 is confirmed!"]
    assert inbox.new_messages() == []                    # never shown twice


def test_inbox_never_shows_send_yourself_messages(tmp_path):
    db, service = inbox_setup(tmp_path)
    service.create_owner_booking(booking_date=date(2026, 10, 1), location_id="julaia", customer_name="Mona",
                                 customer_phone="66666666", payment_choice="full", paid=True)
    inbox = chat.Inbox(db, Conversation("owner", "+96566666666"))
    assert inbox.new_messages() == []
