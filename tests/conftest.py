"""Shared test setup."""

import pytest

from cozysetup import check_instagram, send_outbox, serve_instagram
from cozysetup.settings import MissingApiKey


@pytest.fixture(autouse=True)
def no_real_instagram_token(monkeypatch):
    """Tests never read the real Instagram token from .env - so no command run in a test
    (the outbox sender, the Instagram worker, the connection check) can ever send a real
    Instagram message. Tests that need a sender pass a pretend one."""
    def missing():
        raise MissingApiKey("no Instagram token in tests")
    for module in (send_outbox, serve_instagram, check_instagram):
        monkeypatch.setattr(module, "load_instagram_token", missing)
