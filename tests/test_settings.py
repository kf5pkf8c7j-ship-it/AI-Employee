"""Tests for the API settings - no real API calls."""

import pytest

from cozysetup import check_api
from cozysetup.settings import MODEL, MissingApiKey, estimated_cost_usd, load_api_key


@pytest.fixture
def no_key_in_environment(monkeypatch):
    # setenv first so pytest removes whatever load_dotenv sets during the test.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY")


def test_the_owner_chose_sonnet_5():
    assert MODEL == "claude-sonnet-5"


def test_key_is_read_from_the_env_file(tmp_path, no_key_in_environment):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nANTHROPIC_API_KEY=sk-ant-test-123\n")
    assert load_api_key(env_file) == "sk-ant-test-123"


def test_empty_key_is_reported_clearly(tmp_path, no_key_in_environment):
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=\n")
    with pytest.raises(MissingApiKey, match="console.anthropic.com"):
        load_api_key(env_file)


def test_missing_env_file_is_reported_clearly(tmp_path, no_key_in_environment):
    with pytest.raises(MissingApiKey):
        load_api_key(tmp_path / "does-not-exist")


def test_a_key_already_in_the_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-environment")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=from-file\n")
    assert load_api_key(env_file) == "from-environment"


def test_estimated_cost():
    # Sonnet 5: $2 per million input, $10 per million output.
    assert estimated_cost_usd("claude-sonnet-5", 1_000_000, 0) == pytest.approx(2.00)
    assert estimated_cost_usd("claude-sonnet-5", 0, 1_000_000) == pytest.approx(10.00)
    # Cached reads cost a tenth; cache writes a quarter more.
    assert estimated_cost_usd("claude-sonnet-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.20)
    assert estimated_cost_usd("claude-sonnet-5", 0, 0, cache_write_tokens=1_000_000) == pytest.approx(2.50)


def test_check_api_without_a_key_explains_what_to_do(monkeypatch, capsys):
    def no_key():
        raise MissingApiKey("No Anthropic API key found.")
    monkeypatch.setattr(check_api, "load_api_key", no_key)
    assert check_api.main() == 1
    assert "No Anthropic API key found" in capsys.readouterr().err


def test_env_file_is_never_saved_in_git():
    from pathlib import Path
    gitignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    assert ".env" in gitignore.splitlines()
