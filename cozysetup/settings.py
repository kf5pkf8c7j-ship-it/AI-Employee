"""Settings for talking to the AI model (OpenAI).

The API key is read from the .env file in the project folder (never saved in
git) or from the OPENAI_API_KEY environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"
API_KEY_NAME = "OPENAI_API_KEY"

# The owner chose GPT-6 Sol for the first tests; compare with gpt-6-luna
# (much cheaper) in Step 5 on the same test conversations.
MODEL = "gpt-6-sol"

# US dollars per million tokens - used only to show estimated costs.
# From developers.openai.com/api/docs/pricing (checked September 2026).
PRICE_PER_MILLION = {
    "gpt-6-sol": {"input": 2.00, "cached_input": 0.20, "cache_write": 2.50, "output": 10.00},
    "gpt-6-luna": {"input": 0.10, "cached_input": 0.01, "cache_write": 0.125, "output": 0.50},
}


class MissingApiKey(Exception):
    pass


INSTAGRAM_TOKEN_NAME = "IG_ACCESS_TOKEN"


def load_api_key(env_file: Path = ENV_FILE) -> str:
    """The OpenAI API key, from .env or the environment. Never printed or logged."""
    return _load_secret(env_file, API_KEY_NAME,
                        f"No OpenAI API key found. Open {env_file} in a text editor and put your key "
                        f"after {API_KEY_NAME}= (create a key at platform.openai.com).")


def load_instagram_token(env_file: Path = ENV_FILE) -> str:
    """The Instagram access token, from .env or the environment. Never printed or logged."""
    return _load_secret(env_file, INSTAGRAM_TOKEN_NAME,
                        f"No Instagram access token found. Open {env_file} in a text editor and paste it "
                        f"after {INSTAGRAM_TOKEN_NAME}= (Meta App Dashboard > Instagram > API setup with "
                        "Instagram business login > Generate token).")


def _load_secret(env_file: Path, name: str, missing_message: str) -> str:
    load_dotenv(env_file, override=False)  # a value already in the environment wins
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingApiKey(missing_message)
    return value


def estimated_cost_usd(model: str, input_tokens: int, output_tokens: int,
                       cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    """Approximate cost. `input_tokens` are the uncached ones; cached reads and
    cache writes are counted separately at their own rates."""
    price = PRICE_PER_MILLION[model]
    return (
        input_tokens * price["input"]
        + cache_read_tokens * price["cached_input"]
        + cache_write_tokens * price["cache_write"]
        + output_tokens * price["output"]
    ) / 1_000_000
