"""Check the Instagram connection - read-only: nothing is sent or changed.

    uv run python -m cozysetup.check_instagram

It checks that:
  1. the access token in .env works,
  2. the account is an Instagram professional account (and which one),
  3. the app is allowed to read the account's Direct Messages.
It prints the Instagram account ID to put in .env as IG_ACCOUNT_ID.
The token is never printed.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

from cozysetup.settings import MissingApiKey, load_instagram_token

GRAPH_URL = "https://graph.instagram.com/v25.0"   # Instagram API with Instagram Login
TIMEOUT_SECONDS = 15
PROFESSIONAL_TYPES = {"BUSINESS", "MEDIA_CREATOR"}


class InstagramError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def graph_get(path: str, params: dict, token: str) -> dict:
    """One read-only request to the Instagram Graph API. Errors never contain the token."""
    url = f"{GRAPH_URL}/{path}?" + urllib.parse.urlencode({**params, "access_token": token})
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("error", {})
        except (ValueError, UnicodeDecodeError):
            detail = {}
        message = str(detail.get("message") or f"HTTP {error.code}").replace(token, "…")
        raise InstagramError(message, detail.get("code")) from None
    except (urllib.error.URLError, TimeoutError) as error:
        reason = str(getattr(error, "reason", error)).replace(token, "…")
        raise InstagramError(f"Could not reach Instagram ({reason}). Check the internet connection.") from None


def explain(error: InstagramError) -> str:
    """Turn Meta's error codes into what the owner should do."""
    if error.code == 190:
        return ("The access token was rejected (expired, revoked or copied incompletely). Generate a new one "
                "in the Meta App Dashboard and paste it into .env.")
    if error.code in (10, 200, 230):
        return ("The app doesn't have permission for this. Check that CozySetup.kw approved "
                "instagram_business_manage_messages when you added it, and that 'Allow access to messages' "
                "is on in the Instagram app.")
    if error.code in (4, 17, 32, 613):
        return "Instagram says too many requests right now. Wait a few minutes and try again."
    return str(error)


def main(get: Callable[[str, dict, str], dict] = graph_get) -> int:
    """`get` lets tests use a pretend Instagram instead of the real one."""
    try:
        token = load_instagram_token()
    except MissingApiKey as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1

    # 1 + 2: the token works, and which account it belongs to.
    try:
        me = get("me", {"fields": "user_id,username,name,account_type"}, token)
    except InstagramError as error:
        print(f"✗ {explain(error)}", file=sys.stderr)
        return 1
    account_type = me.get("account_type", "unknown")
    print("✓ The Instagram access token works.")
    print(f"  Account:       @{me.get('username', '?')}" + (f"  ({me['name']})" if me.get("name") else ""))
    print(f"  Account type:  {account_type}")
    print(f"  Account ID:    {me.get('user_id', '?')}   ← put this in .env as IG_ACCOUNT_ID")
    if account_type not in PROFESSIONAL_TYPES:
        print("✗ This is not a professional account. In the Instagram app: Settings > Account type and tools "
              "> Switch to professional account > Business.", file=sys.stderr)
        return 1

    # 3: the app may read Direct Messages (read-only - nothing is sent).
    try:
        conversations = get("me/conversations", {"platform": "instagram", "limit": 5}, token)
    except InstagramError as error:
        print(f"✗ Messages: {explain(error)}", file=sys.stderr)
        return 1
    count = len(conversations.get("data", []))
    shown = f"{count} recent conversation(s) visible" if count else "no conversations yet"
    print(f"✓ Message access works ({shown}). Nothing was sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
