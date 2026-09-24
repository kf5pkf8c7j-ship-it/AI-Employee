"""Settings for talking to Claude.

The API key is read from the .env file in the project folder (never saved in
git) or from the ANTHROPIC_API_KEY environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"

# The owner chose Claude Sonnet 5 to start with (lower cost); compare with
# claude-opus-5 in Step 5 if the test conversations need more.
MODEL = "claude-sonnet-5"

# Price per million tokens, in US dollars - used only to show estimated costs.
PRICE_PER_MILLION = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
}


class MissingApiKey(Exception):
    pass


def load_api_key(env_file: Path = ENV_FILE) -> str:
    """The Anthropic API key, from .env or the environment. Never printed or logged."""
    load_dotenv(env_file, override=False)  # a key already in the environment wins
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise MissingApiKey(
            f"No Anthropic API key found. Open {env_file} in a text editor and put your key "
            "after ANTHROPIC_API_KEY= (create a key at console.anthropic.com)."
        )
    return key


def estimated_cost_usd(model: str, input_tokens: int, output_tokens: int,
                       cache_write_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    """Approximate cost of one request. Cache writes cost 1.25x input, cache reads 0.1x."""
    price = PRICE_PER_MILLION[model]
    input_cost = (input_tokens + cache_write_tokens * 1.25 + cache_read_tokens * 0.1) * price["input"]
    return (input_cost + output_tokens * price["output"]) / 1_000_000
