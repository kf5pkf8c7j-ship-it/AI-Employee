"""Check that the Anthropic API key works, with one tiny request.

    uv run python -m cozysetup.check_api
"""

from __future__ import annotations

import sys

import anthropic

from cozysetup.settings import MODEL, MissingApiKey, estimated_cost_usd, load_api_key


def main() -> int:
    try:
        client = anthropic.Anthropic(api_key=load_api_key())
    except MissingApiKey as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with exactly: Hello from CozySetup"}],
        )
    except anthropic.AuthenticationError:
        print("✗ The API key was rejected. Check it was copied completely into .env.", file=sys.stderr)
        return 1
    except anthropic.PermissionDeniedError:
        print("✗ The key has no permission for this. Check your account in the console.", file=sys.stderr)
        return 1
    except anthropic.RateLimitError:
        print("✗ Rate limited or out of credit. Check billing at console.anthropic.com.", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as error:
        print(f"✗ API error {error.status_code}: {error.message}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError:
        print("✗ Could not reach the API. Check the internet connection.", file=sys.stderr)
        return 1

    reply = next((block.text for block in response.content if block.type == "text"), "(no text)")
    usage = response.usage
    cost = estimated_cost_usd(response.model, usage.input_tokens, usage.output_tokens)
    print("✓ The API key works.")
    print(f"  Model:  {response.model}")
    print(f"  Reply:  {reply}")
    print(f"  Tokens: {usage.input_tokens} in, {usage.output_tokens} out  (about ${cost:.5f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
