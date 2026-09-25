"""The whole journey, across every part we built (Step 6.5).

One database file. Real agent, tools, booking service, admin commands, outbox
sender command, log sender and chat inbox - only the AI model (pretend) and
the clock (movable) are not real. No API calls, no cost.
"""

from datetime import date, datetime, timedelta

import pytest

from cozysetup import admin, outbox, send_outbox
from cozysetup.agent import Agent
from cozysetup.bookings import BookingService
from cozysetup.business_info import load_business_info
from cozysetup.chat import Inbox
from cozysetup.database import BookingStatus, HandoffType, Language, OutboxKind, OutboxStatus, connect
from cozysetup.senders import LOG_FILE_NAME, SendError
from cozysetup.tools import Conversation
from tests.test_agent import PretendOpenAI, answer, call, text

INFO = load_business_info()
KUWAIT = INFO.timezone
THURSDAY = date(2026, 10, 1)
JPG = b"\xff\xd8\xff\xe0" + bytes(100)
BOOKING = {"date": "2026-10-01", "location_id": "julaia", "customer_name": "Ahmad",
           "customer_phone": "99999999", "payment_choice": "deposit"}

CONFIRMATION = ("Your booking CS-0001 is confirmed!\n"
                "Julaia, Thursday 1 October 2026, 6 PM – 11 PM.\n"
                "On the booking day, please pay: 25 KWD remaining + 20 KWD security deposit.")
REMINDER = ("Reminder: your CozySetup booking is today at Julaia, from 6 PM to 11 PM.\n"
            "Please remember to pay on arrival: 25 KWD remaining + 20 KWD security deposit.")


def at(day, hour, minute=0):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KUWAIT)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock(at(date(2026, 9, 28), 14))        # Monday 2 PM


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "cozysetup.db"


@pytest.fixture
def service(db_path, clock):
    db = connect(db_path)
    yield BookingService(db, INFO, clock=clock, proofs_dir=db_path.parent / "payment_proofs")
    db.close()


def owner(db_path, clock, *argv):
    """The owner types an admin command (answering yes)."""
    return admin.main(["--db", str(db_path), *argv, "-y"], clock=clock)


def outbox_run(db_path, clock, senders=None):
    return send_outbox.main(["--db", str(db_path), "run"], clock=clock, senders=senders)


def customer_books_through_the_ai(service):
    """The customer chats with the AI employee and books, then sends a payment screenshot."""
    model = PretendOpenAI(
        answer(call("check_availability", date="2026-10-01")),
        answer(text("You can pay the full amount (50 KWD) now, or a 50% deposit (25 KWD) now…")),
        answer(text("Please send your name and phone number.")),
        answer(call("show_booking_summary", **BOOKING)), answer(text("<summary>")),
        answer(call("create_booking", **BOOKING, customer_language="en")), answer(text("<payment instructions>")),
        answer(call("attach_payment_proof", reference="CS-0001", customer_phone="99999999", attachment_number=1)),
        answer(text("Thank you, we've received your screenshot.")),
    )
    conversation = Conversation("terminal", "customer-1")
    agent = Agent(model, service, conversation)
    agent.reply("I want to book Julaia on Thursday 1 October")
    agent.reply("The deposit")
    agent.reply("Ahmad, 99999999")
    agent.reply("Yes, correct")
    agent.reply("Here is my transfer", images=[JPG])
    return conversation


def statuses(service):
    return {m.kind.value: m.status.value for m in outbox.messages_for(service.db, 1)}


# --- The full journey ---------------------------------------------------------------------

def test_the_whole_journey_from_first_message_to_completed(service, db_path, clock, capsys):
    # 1. The customer books through the AI, and sends a screenshot.
    conversation = customer_books_through_the_ai(service)
    booking = service.get_booking("CS-0001")
    assert booking.status is BookingStatus.PAYMENT_SUBMITTED
    assert booking.language is Language.ENGLISH
    assert booking.channel_user_id == "customer-1"
    assert outbox.messages_for(service.db, booking.id) == []            # nothing before approval
    inbox = Inbox(service.db, conversation)

    # 2. The owner checks the account and approves.
    assert owner(db_path, clock, "approve", "CS-0001", "25 KWD received") == 0
    out = capsys.readouterr().out
    assert "✓ Confirmation queued for the customer (terminal) - it goes out with cozysetup-outbox." in out
    assert "Reminder queued for Thu 1 Oct, 3 PM." in out
    assert statuses(service) == {"confirmation": "pending", "reminder": "pending"}
    assert inbox.new_messages() == []                                   # queued, not delivered

    # 3. The outbox sender runs: the confirmation is delivered.
    assert outbox_run(db_path, clock) == 0
    assert statuses(service) == {"confirmation": "sent", "reminder": "pending"}
    log = db_path.parent / LOG_FILE_NAME
    assert log.read_text(encoding="utf-8") == (
        "2026-09-28 14:00  terminal → customer-1\n"
        + "".join(f"  {line}\n" for line in CONFIRMATION.splitlines()) + "\n")
    assert [m.text for m in inbox.new_messages()] == [CONFIRMATION]    # what the customer sees

    # 4. The booking day: nothing at 14:59, the reminder at 15:00.
    clock.now = at(THURSDAY, 14, 59)
    outbox_run(db_path, clock)
    assert statuses(service)["reminder"] == "pending"
    clock.now = at(THURSDAY, 15, 0)
    outbox_run(db_path, clock)
    assert statuses(service) == {"confirmation": "sent", "reminder": "sent"}
    assert [m.text for m in inbox.new_messages()] == [REMINDER]
    assert log.read_text(encoding="utf-8").endswith(
        "2026-10-01 15:00  terminal → customer-1\n" + "".join(f"  {line}\n" for line in REMINDER.splitlines()) + "\n")

    # 5. After the setup, the owner marks it completed.
    clock.now = at(THURSDAY, 23, 30)
    assert owner(db_path, clock, "complete", "CS-0001") == 0
    assert service.get_booking("CS-0001").status is BookingStatus.COMPLETED

    # 6. The history tells the whole story, in order.
    story = [(e["actor"], e["event"], e["new_status"] or e["details"]) for e in service.booking_history("CS-0001")]
    assert story == [
        ("ai", "created", "pending_payment"),
        ("ai", "status_changed", "payment_submitted"),
        ("owner", "status_changed", "confirmed"),
        ("system", "outbox", "confirmation queued via terminal, from 28 Sep 14:00"),
        ("system", "outbox", "reminder queued via terminal, from 01 Oct 15:00"),
        ("system", "outbox", "confirmation sent via terminal to customer-1"),
        ("system", "outbox", "reminder sent via terminal to customer-1"),
        ("owner", "status_changed", "completed"),
    ]
    assert service.list_handoffs() == []


def test_cancelled_before_the_reminder_the_reminder_is_never_sent(service, db_path, clock, capsys):
    conversation = customer_books_through_the_ai(service)
    inbox = Inbox(service.db, conversation)
    owner(db_path, clock, "approve", "CS-0001")
    outbox_run(db_path, clock)                                          # confirmation delivered
    assert len(inbox.new_messages()) == 1

    clock.now = at(date(2026, 9, 29), 10)
    capsys.readouterr()
    assert owner(db_path, clock, "cancel", "CS-0001", "customer changed plans") == 0
    assert "1 waiting message cancelled. Tell the customer about the cancellation yourself." in (
        capsys.readouterr().out)

    clock.now = at(THURSDAY, 15)
    outbox_run(db_path, clock)
    assert statuses(service) == {"confirmation": "sent", "reminder": "cancelled"}
    assert inbox.new_messages() == []
    assert (db_path.parent / LOG_FILE_NAME).read_text(encoding="utf-8").count("terminal → customer-1") == 1


class BrokenChannel:
    def __init__(self):
        self.calls = 0

    def send(self, recipient, text):
        self.calls += 1
        raise SendError("network unreachable")


def test_three_failed_attempts_hand_it_to_the_owner_and_cancel_the_reminder(service, db_path, clock, capsys):
    conversation = customer_books_through_the_ai(service)
    owner(db_path, clock, "approve", "CS-0001")
    broken = {"terminal": BrokenChannel()}

    assert outbox_run(db_path, clock, broken) == 0                      # attempt 1: retry later
    clock.now += timedelta(minutes=5)
    assert outbox_run(db_path, clock, broken) == 0                      # attempt 2: retry later
    clock.now += timedelta(minutes=30)
    assert outbox_run(db_path, clock, broken) == 2                      # attempt 3: failed
    assert broken["terminal"].calls == 3
    assert statuses(service) == {"confirmation": "failed", "reminder": "cancelled"}

    [handoff] = service.list_handoffs()
    assert handoff.type is HandoffType.OTHER
    assert handoff.summary.startswith("Could not send the confirmation for CS-0001 to customer-1 via terminal "
                                      "after 3 attempts (last error: network unreachable).")

    capsys.readouterr()
    admin.main(["--db", str(db_path), "overview"], clock=clock)     # looking only: no -y
    assert "1 message(s) failed to send - see: outbox, handoffs" in capsys.readouterr().out

    # The owner calls the customer, then records it - the handoff stays open until resolved.
    confirmation = next(m for m in outbox.messages_for(service.db, 1) if m.kind is OutboxKind.CONFIRMATION)
    assert owner(db_path, clock, "mark-sent", str(confirmation.id), "called the customer") == 0
    assert statuses(service) == {"confirmation": "sent", "reminder": "cancelled"}
    assert len(service.list_handoffs()) == 1
    assert Inbox(service.db, conversation).new_messages()[0].status is OutboxStatus.SENT
