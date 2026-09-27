"""End to end on Instagram (Step 7.6): one customer, from the first DM to the reminder.

Everything is the real code - the signed webhook, the worker, the booking rules, the
owner's admin commands and the outbox - with only the outside world pretended: Meta
(signed webhook calls), the AI model and Instagram's API. Made-up accounts, a
temporary database; nothing is sent anywhere.
"""

import hashlib
import hmac
import json

import pytest
from starlette.testclient import TestClient

from cozysetup import admin, instagram_worker, outbox
from cozysetup.database import BookingStatus, OutboxKind, OutboxStatus
from cozysetup.instagram_webhook import create_app
from cozysetup.settings import WebhookSettings
from tests.test_agent import BOOKING, PretendOpenAI, answer, call, text
from tests.test_instagram_outbox import PretendInstagram
from tests.test_instagram_test_mode import Profiles
from tests.test_instagram_worker import ACCOUNT, CUSTOMER, DISCLOSURE, JPG, OTHER_CUSTOMER, Setup, image

SECRET = "test-app-secret"
TESTER = ("tester.one", "Tester")
STRANGER = ("a.real.customer", "Stranger")


class Meta:
    """Meta calling our webhook: signed deliveries, sometimes the same one twice."""

    def __init__(self, setup):
        self.setup = setup
        app = create_app(WebhookSettings(app_secret=SECRET, verify_token="v", account_id=ACCOUNT), setup.path,
                         clock=setup.clock)
        self.http = TestClient(app)

    def deliver(self, *events):
        body = json.dumps({"object": "instagram",
                           "entry": [{"id": ACCOUNT, "time": 1, "messaging": list(events)}]}).encode()
        signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = self.http.post("/webhooks/instagram", content=body,
                                  headers={"Content-Type": "application/json", "X-Hub-Signature-256": signature})
        assert response.status_code == 200
        return body, signature


def admin_run(s, *argv, opened=None):
    return admin.main(["--db", str(s.path), *argv], clock=s.clock, ask=lambda question: "y",
                      open_file=(opened.append if opened is not None else lambda path: None))


def texts_sent(s, customer=CUSTOMER):
    return [words for to, words in s.sender.sent if to == customer]


@pytest.fixture
def s(tmp_path):
    model = PretendOpenAI(
        answer(text("The setup is 50 KWD from 6 PM to 11 PM.")),                          # 1. price question
        answer(call("show_booking_summary", **BOOKING)), answer(text("Please check your booking details...")),
        answer(call("create_booking", call_id="c2", **BOOKING, customer_language="en")),
        answer(text("Your booking CS-0001 has been created. Please transfer 25 KWD via Wamd.")),
        answer(call("attach_payment_proof", call_id="c3", reference="CS-0001", customer_phone="99999999",
                    attachment_number=1)),
        answer(text("Thank you, we've received your screenshot.")),
        answer(text("You're welcome - see you on Thursday!")),                             # after the reminder
    )
    setup = Setup(tmp_path / "cozysetup.db", client=model,
                  profiles=Profiles({CUSTOMER: TESTER, OTHER_CUSTOMER: STRANGER}), only={"@tester.one"})
    yield setup
    setup.close()


def test_one_instagram_customer_from_first_dm_to_reminder(s, capsys):
    meta = Meta(s)

    # 1. The first DM - and Meta delivering it twice, as it sometimes does.
    first = s.dm("Hi, how much is the setup?")
    meta.deliver(first)
    meta.deliver(first)
    s.worker.run_once()
    assert texts_sent(s) == [DISCLOSURE, "The setup is 50 KWD from 6 PM to 11 PM."]
    assert s.conversation().username == "tester.one"

    # A stranger writes at the same time: test mode - never an AI reply.
    meta.deliver(s.dm("Is Friday free?", customer=OTHER_CUSTOMER))
    s.worker.run_once()
    assert texts_sent(s, OTHER_CUSTOMER) == []

    # 2. Booking: details, summary, "yes".
    meta.deliver(s.dm("Julaia, 1 October, deposit, Ahmad, 99999999"))
    s.worker.run_once()
    meta.deliver(s.dm("yes"))
    s.worker.run_once()
    booking = s.service.get_booking("CS-0001")
    assert booking.status is BookingStatus.PENDING_PAYMENT
    assert (booking.channel, booking.channel_user_id) == ("instagram", CUSTOMER)

    # 3. The payment screenshot arrives as an Instagram image.
    meta.deliver(s.dm(attachments=image()))
    s.worker.run_once()
    booking = s.service.get_booking("CS-0001")
    assert booking.status is BookingStatus.PAYMENT_SUBMITTED
    assert (s.service.proofs_dir / booking.payment_proof).read_bytes() == JPG

    # 4. The owner checks and approves.
    assert admin_run(s, "pending") == 0
    opened = []
    assert admin_run(s, "proof", "CS-0001", opened=opened) == 0
    assert opened == [s.service.proofs_dir / booking.payment_proof]
    assert admin_run(s, "approve", "CS-0001", "25 KWD received") == 0
    assert "Confirmation queued for Instagram" in capsys.readouterr().out

    # 5. The outbox sends the confirmation inside the 24-hour window; its echo is ours.
    instagram = PretendInstagram()
    assert [r.outcome for r in outbox.deliver_due(s.service, {"instagram": instagram})] == ["sent"]
    confirmation = next(m for m in outbox.messages_for(s.db, booking.id) if m.kind is OutboxKind.CONFIRMATION)
    meta.deliver(s.dm(confirmation.text, echo=True, mid=confirmation.external_id))
    [result] = s.worker.run_once()
    assert result.what == "echo" and not s.conversation().ai_paused

    # 6. The booking day: the window has closed, so the reminder is for the owner.
    s.clock.now = outbox.reminder_time(s.service.info, booking)
    assert [r.outcome for r in outbox.deliver_due(s.service, {"instagram": instagram})] == ["send_yourself"]
    assert len(instagram.sent) == 1
    reminder = next(m for m in outbox.messages_for(s.db, booking.id) if m.kind is OutboxKind.REMINDER)

    # 7. The owner copies it into the Instagram app: marked sent automatically, AI still answering.
    meta.deliver(s.dm(reminder.text, echo=True, mid="mid.typed.by.the.owner"))
    results = {r.customer: r.what for r in s.worker.run_once()}
    assert results == {CUSTOMER: "marked_sent",
                       OTHER_CUSTOMER: "ignored"}     # the stranger's Monday message is now over 24 hours old
    reminder = outbox.get_message(s.db, reminder.id)
    assert reminder.status is OutboxStatus.SENT and reminder.external_id == "mid.typed.by.the.owner"
    assert not s.conversation().ai_paused

    # 8. The customer thanks them - a new window, and the AI answers.
    meta.deliver(s.dm("Thanks!"))
    s.worker.run_once()
    assert texts_sent(s)[-1] == "You're welcome - see you on Thursday!"

    # Everything happened exactly once.
    assert s.db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
    assert s.db.execute("SELECT COUNT(*) FROM inbound_messages WHERE external_id = ?",
                        (first["message"]["mid"],)).fetchone()[0] == 1
    assert texts_sent(s).count(DISCLOSURE) == 1
    assert s.service.list_handoffs() == []
    assert s.db.execute("SELECT status FROM inbound_messages WHERE channel_user_id = ?",
                        (OTHER_CUSTOMER,)).fetchone()["status"] == "ignored"   # never answered by the AI


def test_a_retry_after_a_failed_save_does_not_create_a_second_booking(tmp_path, monkeypatch):
    model = PretendOpenAI(
        answer(call("show_booking_summary", **BOOKING)), answer(text("Please check your booking details...")),
        answer(call("create_booking", call_id="c1", **BOOKING, customer_language="en")),
        answer(text("Your booking CS-0001 has been created.")),
        # The retry: the model asks again, with the same details.
        answer(call("create_booking", call_id="c1", **BOOKING, customer_language="en")),
        answer(text("Your booking CS-0001 has been created.")),
    )
    s = Setup(tmp_path / "cozysetup.db", client=model, profiles=Profiles({CUSTOMER: TESTER}), only={"tester.one"})
    try:
        s.receive(s.dm("Julaia, 1 October, deposit, Ahmad, 99999999"))
        s.worker.run_once()                                            # the summary
        s.receive(s.dm("yes, book it"))
        real_split = instagram_worker.split_text
        failures = [RuntimeError("disk full")]

        def split_once_broken(words, *args):
            if failures:
                raise failures.pop()
            return real_split(words, *args)
        monkeypatch.setattr(instagram_worker, "split_text", split_once_broken)

        [result] = s.worker.run_once()
        assert result.what == "retry"                                  # the booking exists, the reply wasn't saved
        s.clock.advance(minutes=1)
        s.worker.run_once()
        assert s.db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1
        assert texts_sent(s)[-1] == "Your booking CS-0001 has been created."
        events = [row["event"] for row in s.service.booking_history("CS-0001")]
        assert events == ["created", "duplicate_request"]
    finally:
        s.close()
