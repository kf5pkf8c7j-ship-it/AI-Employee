"""Tests for the terminal chat, with a pretend model and pretend typing - no cost."""

from datetime import datetime

from cozysetup import chat
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
