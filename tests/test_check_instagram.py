"""Tests for the read-only Instagram connection check - pretend Instagram, no network."""

import io
import json
import urllib.error

import pytest

from cozysetup import check_instagram
from cozysetup.check_instagram import InstagramError, graph_get
from cozysetup.settings import MissingApiKey

TOKEN = "SECRET-TOKEN-123"
ME = {"user_id": "17841400000000000", "username": "cozysetup.kw", "name": "CozySetup", "account_type": "BUSINESS"}


class PretendInstagram:
    def __init__(self, me=ME, conversations=None, errors=None):
        self.me, self.conversations = me, conversations or {"data": [{"id": "c1"}, {"id": "c2"}]}
        self.errors = errors or {}
        self.calls = []

    def __call__(self, path, params, token):
        self.calls.append((path, params, token))
        if path in self.errors:
            raise self.errors[path]
        return self.me if path == "me" else self.conversations


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setattr(check_instagram, "load_instagram_token", lambda: TOKEN)


def run(capsys, instagram):
    code = check_instagram.main(get=instagram)
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_a_working_professional_account(capsys):
    instagram = PretendInstagram()
    code, out = run(capsys, instagram)
    assert code == 0
    assert "✓ The Instagram access token works." in out
    assert "Account:       @cozysetup.kw  (CozySetup)" in out
    assert "Account type:  BUSINESS" in out
    assert "Account ID:    17841400000000000   ← put this in .env as IG_ACCOUNT_ID" in out
    assert "✓ Message access works (2 recent conversation(s) visible). Nothing was sent." in out


def test_only_reads(capsys):
    instagram = PretendInstagram()
    run(capsys, instagram)
    assert [(path, params) for path, params, _ in instagram.calls] == [
        ("me", {"fields": "user_id,username,name,account_type"}),
        ("me/conversations", {"platform": "instagram", "limit": 5}),
    ]


def test_no_conversations_yet_is_fine(capsys):
    code, out = run(capsys, PretendInstagram(conversations={"data": []}))
    assert code == 0 and "no conversations yet" in out


def test_a_creator_account_is_professional_too(capsys):
    assert run(capsys, PretendInstagram(me={**ME, "account_type": "MEDIA_CREATOR"}))[0] == 0


def test_a_personal_account_is_refused(capsys):
    code, out = run(capsys, PretendInstagram(me={**ME, "account_type": "PERSONAL"}))
    assert code == 1
    assert "not a professional account" in out


def test_a_rejected_token_is_explained(capsys):
    code, out = run(capsys, PretendInstagram(errors={"me": InstagramError("Invalid OAuth access token", 190)}))
    assert code == 1
    assert "The access token was rejected" in out


def test_missing_message_permission_is_explained(capsys):
    error = InstagramError("(#10) Application does not have permission", 10)
    code, out = run(capsys, PretendInstagram(errors={"me/conversations": error}))
    assert code == 1
    assert "✗ Messages: The app doesn't have permission" in out
    assert "Allow access to messages" in out


def test_missing_token(monkeypatch, capsys):
    def none():
        raise MissingApiKey("No Instagram access token found.")
    monkeypatch.setattr(check_instagram, "load_instagram_token", none)
    code, out = run(capsys, PretendInstagram())
    assert code == 1 and "No Instagram access token found." in out


def test_the_token_is_never_printed(capsys):
    code, out = run(capsys, PretendInstagram())
    assert TOKEN not in out


# --- The real request function never leaks the token in its errors --------------------

def test_http_error_message_never_contains_the_token(monkeypatch):
    body = json.dumps({"error": {"message": f"Bad token {TOKEN}", "code": 190}}).encode()

    def fail(url, timeout):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(body))
    monkeypatch.setattr(check_instagram.urllib.request, "urlopen", fail)
    with pytest.raises(InstagramError) as error:
        graph_get("me", {}, TOKEN)
    assert error.value.code == 190
    assert TOKEN not in str(error.value)


def test_network_error_never_contains_the_token(monkeypatch):
    def fail(url, timeout):
        raise urllib.error.URLError(f"failed for {url}")
    monkeypatch.setattr(check_instagram.urllib.request, "urlopen", fail)
    with pytest.raises(InstagramError) as error:
        graph_get("me", {}, TOKEN)
    assert TOKEN not in str(error.value)
    assert "Could not reach Instagram" in str(error.value)


def test_requests_use_instagram_login_and_a_timeout(monkeypatch):
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def capture(url, timeout):
        seen["url"], seen["timeout"] = url, timeout
        return Response(json.dumps(ME).encode())
    monkeypatch.setattr(check_instagram.urllib.request, "urlopen", capture)
    assert graph_get("me", {"fields": "user_id"}, TOKEN) == ME
    assert seen["url"].startswith("https://graph.instagram.com/v25.0/me?")
    assert seen["timeout"] == 15
