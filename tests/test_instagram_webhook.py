"""Tests for the Instagram webhook receiver (Step 7.3) - sample Meta payloads, signed here.
No network, no Meta, no AI."""

import ast
import hashlib
import hmac
import json
import logging
from datetime import datetime

import pytest
from starlette.testclient import TestClient

from cozysetup import instagram_webhook
from cozysetup.database import connect
from cozysetup.instagram_webhook import MAX_BODY_BYTES, create_app, read_events, signature_is_valid
from cozysetup.settings import WebhookSettings

SECRET = "test-app-secret"
VERIFY = "test-verify-token"
ACCOUNT = "17841461464339502"
CUSTOMER = "1234567890123456"
NOW = datetime(2026, 9, 28, 14, 0).astimezone()
SETTINGS = WebhookSettings(app_secret=SECRET, verify_token=VERIFY, account_id=ACCOUNT)


# --- Sample payloads in Meta's messaging format --------------------------------------

def event(mid="mid.1", text="Hi, how much is the setup?", *, attachments=None, echo=False, **extra):
    message = {"mid": mid, **({"text": text} if text is not None else {}), **extra}
    if attachments:
        message["attachments"] = attachments
    if echo:
        message["is_echo"] = True
    sender, recipient = (ACCOUNT, CUSTOMER) if echo else (CUSTOMER, ACCOUNT)
    return {"sender": {"id": sender}, "recipient": {"id": recipient}, "timestamp": 1759057200000, "message": message}


def payload(*events, account=ACCOUNT):
    return {"object": "instagram", "entry": [{"id": account, "time": 1759057200, "messaging": list(events)}]}


def sign(body: bytes, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "cozysetup.db"
    connect(path).close()
    return path


@pytest.fixture
def client(db_path):
    return TestClient(create_app(SETTINGS, db_path, clock=lambda: NOW))


def post(client, data, *, signature=None, raw=None):
    body = raw if raw is not None else json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if signature is not False:
        headers["X-Hub-Signature-256"] = signature or sign(body)
    return client.post("/webhooks/instagram", content=body, headers=headers)


def stored(db_path):
    db = connect(db_path)
    rows = [dict(r) for r in db.execute("SELECT * FROM inbound_messages ORDER BY id")]
    db.close()
    return rows


# --- Verification (Meta's one-time handshake) ---------------------------------------------

def test_verification_with_the_right_token_returns_the_challenge(client):
    response = client.get("/webhooks/instagram", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "1158201444"})
    assert (response.status_code, response.text) == (200, "1158201444")


@pytest.mark.parametrize("params", [
    {"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "1"},
    {"hub.mode": "unsubscribe", "hub.verify_token": VERIFY, "hub.challenge": "1"},
    {"hub.mode": "subscribe", "hub.challenge": "1"},
    {"hub.mode": "subscribe", "hub.verify_token": VERIFY},
    {},
], ids=["wrong token", "wrong mode", "no token", "no challenge", "nothing"])
def test_verification_is_refused_otherwise(client, params):
    response = client.get("/webhooks/instagram", params=params)
    assert response.status_code == 403
    assert VERIFY not in response.text


# --- The signature -------------------------------------------------------------------------

def test_a_correctly_signed_message_is_saved(client, db_path):
    response = post(client, payload(event()))
    assert (response.status_code, response.text) == (200, "EVENT_RECEIVED")
    [row] = stored(db_path)
    assert (row["channel"], row["external_id"], row["channel_user_id"], row["status"]) == (
        "instagram", "mid.1", CUSTOMER, "waiting")
    assert row["received_at"] == NOW.isoformat(timespec="seconds")
    assert json.loads(row["payload"])["message"]["text"] == "Hi, how much is the setup?"


@pytest.mark.parametrize("signature", [
    False,                                                     # no header at all
    "sha256=" + "0" * 64,                                      # wrong value
    sign(b"a different body"),                                 # signature of another body
    sign(json.dumps(payload(event())).encode(), secret="not-our-secret"),
    "sha1=" + hashlib.sha1(b"x").hexdigest(),                  # old/other algorithm
    "sha256=",                                                 # empty
], ids=["missing", "wrong", "other body", "other secret", "sha1 prefix", "empty"])
def test_a_bad_signature_is_refused_and_nothing_saved(client, db_path, signature):
    assert post(client, payload(event()), signature=signature).status_code == 403
    assert stored(db_path) == []


def test_changing_a_single_byte_breaks_the_signature(client, db_path):
    body = json.dumps(payload(event(text="Price: 50 KWD"))).encode()
    tampered = body.replace(b"50", b"05")
    assert post(client, None, raw=tampered, signature=sign(body)).status_code == 403
    assert stored(db_path) == []


def test_signature_check_itself():
    body = b'{"a": 1}'
    assert signature_is_valid(SECRET, body, sign(body))
    assert not signature_is_valid(SECRET, body, None)
    assert not signature_is_valid(SECRET, body + b" ", sign(body))


# --- Deduplication by message id ---------------------------------------------------------------

def test_the_same_message_twice_is_saved_once_and_answered_200_both_times(client, db_path):
    assert post(client, payload(event(mid="mid.A"))).status_code == 200
    assert post(client, payload(event(mid="mid.A"))).status_code == 200      # Meta retrying
    assert [r["external_id"] for r in stored(db_path)] == ["mid.A"]


def test_a_batch_saves_every_new_message_once(client, db_path):
    post(client, payload(event(mid="mid.A")))
    post(client, payload(event(mid="mid.A"), event(mid="mid.B", text="Is Thursday free?"), event(mid="mid.C")))
    assert [r["external_id"] for r in stored(db_path)] == ["mid.A", "mid.B", "mid.C"]


def test_the_log_counts_new_duplicates_and_skipped(client, caplog):
    post(client, payload(event(mid="mid.A")))
    with caplog.at_level(logging.INFO, logger="cozysetup.instagram"):
        post(client, payload(event(mid="mid.A"), event(mid="mid.B"), {"sender": {"id": CUSTOMER}, "read": {"mid": "x"}}))
    assert "received 1 new, 1 duplicate; skipped: 1 not a message" in caplog.text


# --- What is saved and what is skipped ----------------------------------------------------------

def test_an_image_is_saved_with_its_url_for_later_download(client, db_path):
    url = "https://lookaside.fbsbx.com/ig_messaging_cdn/?asset_id=123&signature=abc"
    post(client, payload(event(text=None, attachments=[{"type": "image", "payload": {"url": url}}])))
    [row] = stored(db_path)
    assert json.loads(row["payload"])["message"]["attachments"][0]["payload"]["url"] == url


@pytest.mark.parametrize("attachment", [
    {"type": "audio", "payload": {"url": "https://lookaside.fbsbx.com/a"}},
    {"type": "video", "payload": {"url": "https://lookaside.fbsbx.com/v"}},
    {"type": "like_heart"},
    {"type": "ig_reel", "payload": {"reel_video_id": "1"}},
    {"type": "reel", "payload": {"url": "https://www.instagram.com/reel/x"}},
    {"type": "share", "payload": {"url": "https://www.instagram.com/p/x"}},
    {"type": "story_mention", "payload": {"url": "https://lookaside.fbsbx.com/s"}},
])
def test_unsupported_types_are_saved_as_waiting_for_the_worker_to_answer(client, db_path, attachment):
    post(client, payload(event(text=None, attachments=[attachment])))
    [row] = stored(db_path)
    assert row["status"] == "waiting"
    assert json.loads(row["payload"])["message"]["attachments"] == [attachment]


def test_a_story_reply_is_saved(client, db_path):
    post(client, payload(event(reply_to={"story": {"url": "https://lookaside.fbsbx.com/s", "id": "9"}})))
    assert len(stored(db_path)) == 1


def test_an_echo_is_saved_with_the_customer_as_recipient(client, db_path):
    post(client, payload(event(mid="mid.echo", text="I'll check and get back to you", echo=True)))
    [row] = stored(db_path)
    assert row["channel_user_id"] == CUSTOMER                    # not our own account id
    assert json.loads(row["payload"])["message"]["is_echo"] is True


@pytest.mark.parametrize("skipped_event", [
    {"sender": {"id": CUSTOMER}, "recipient": {"id": ACCOUNT}, "read": {"mid": "mid.1"}},
    {"sender": {"id": CUSTOMER}, "recipient": {"id": ACCOUNT}, "reaction": {"mid": "mid.1", "reaction": "love"}},
    {"sender": {"id": CUSTOMER}, "recipient": {"id": ACCOUNT}, "postback": {"title": "Start", "payload": "S"}},
    event(is_deleted=True),
    {"sender": {"id": CUSTOMER}, "recipient": {"id": ACCOUNT}, "message": {"text": "no mid"}},
], ids=["read receipt", "reaction", "button click", "deleted", "no message id"])
def test_other_events_are_skipped_but_answered_200(client, db_path, skipped_event):
    assert post(client, payload(skipped_event)).status_code == 200
    assert stored(db_path) == []


def test_events_for_another_account_are_skipped(client, db_path):
    assert post(client, payload(event(), account="99999999999")).status_code == 200
    assert stored(db_path) == []


def test_the_changes_form_is_accepted_too():
    reading = read_events({"object": "instagram", "entry": [
        {"id": ACCOUNT, "changes": [{"field": "messages", "value": event(mid="mid.X")}]}]}, ACCOUNT)
    assert [m.external_id for m in reading.messages] == ["mid.X"]


# --- Robustness ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", "ÿ".encode("latin-1")])
def test_signed_but_not_valid_json_is_refused(client, db_path, raw):
    assert post(client, None, raw=raw).status_code == 400
    assert stored(db_path) == []


def test_an_oversized_body_is_refused(client, db_path):
    body = b'{"x": "' + b"a" * MAX_BODY_BYTES + b'"}'
    assert post(client, None, raw=body).status_code == 413
    assert stored(db_path) == []


def test_a_database_failure_answers_500_so_meta_retries(client, db_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(instagram_webhook, "store", broken)
    assert post(client, payload(event())).status_code == 500
    assert stored(db_path) == []


def test_a_failing_batch_saves_nothing_at_all(db_path):
    db = connect(db_path)
    good = instagram_webhook.Inbound("mid.1", CUSTOMER, False, event())
    bad = instagram_webhook.Inbound("mid.2", None, False, event())       # violates NOT NULL
    with pytest.raises(Exception):
        instagram_webhook.store(db, [good, bad], NOW)
    db.close()
    assert stored(db_path) == []                                           # rolled back as a whole


def test_health(client):
    assert (client.get("/health").status_code, client.get("/health").text) == (200, "ok")


def test_the_receiver_never_calls_the_ai_or_downloads():
    tree = ast.parse(instagram_webhook.__loader__.get_source(instagram_webhook.__name__))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    for forbidden in ("openai", "cozysetup.agent", "cozysetup.tools", "urllib.request", "httpx", "requests"):
        assert forbidden not in imported


def test_secrets_never_appear_in_responses_or_logs(client, caplog):
    with caplog.at_level(logging.DEBUG, logger="cozysetup.instagram"):
        responses = [
            post(client, payload(event()), signature="sha256=" + "0" * 64),
            post(client, payload(event())),
            client.get("/webhooks/instagram", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                                      "hub.challenge": "1"}),
        ]
    everything = caplog.text + "".join(r.text for r in responses)
    assert SECRET not in everything and VERIFY not in everything
