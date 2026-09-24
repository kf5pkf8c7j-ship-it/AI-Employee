"""Check that the OpenAI API key works, with one tiny request.

    uv run python -m cozysetup.check_api
"""

from __future__ import annotations

import sys

import openai

from cozysetup.settings import MODEL, MissingApiKey, estimated_cost_usd, load_api_key


def main() -> int:
    try:
        client = openai.OpenAI(api_key=load_api_key())
    except MissingApiKey as error:
        print(f"✗ {error}", file=sys.stderr)
        return 1

    try:
        response = client.responses.create(
            model=MODEL,
            input="Reply with exactly: Hello from CozySetup",
            reasoning={"effort": "low"},
            store=False,
            max_output_tokens=1024,
        )
    except openai.AuthenticationError:
        print("✗ The API key was rejected. Check it was copied completely into .env.", file=sys.stderr)
        return 1
    except openai.PermissionDeniedError:
        print("✗ The key has no permission for this model. Check your project at platform.openai.com.",
              file=sys.stderr)
        return 1
    except openai.RateLimitError:
        print("✗ Rate limited or out of credit. Check billing at platform.openai.com.", file=sys.stderr)
        return 1
    except openai.APIStatusError as error:
        print(f"✗ API error {error.status_code}: {error.message}", file=sys.stderr)
        return 1
    except openai.APIConnectionError:
        print("✗ Could not reach the API. Check the internet connection.", file=sys.stderr)
        return 1

    usage = response.usage
    cost = estimated_cost_usd(MODEL, usage.input_tokens, usage.output_tokens)
    print("✓ The API key works.")
    print(f"  Model:  {response.model}")
    print(f"  Reply:  {response.output_text or '(no text)'}")
    print(f"  Tokens: {usage.input_tokens} in, {usage.output_tokens} out  (about ${cost:.5f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
