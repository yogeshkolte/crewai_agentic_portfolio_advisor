"""Call DeepSeek's deepseek-flash model through its OpenAI-compatible API.

Setup:
    pip install openai
    export DEEPSEEK_API_KEY="sk-..."      # from https://platform.deepseek.com/api_keys

Run:
    python deepseek_flash.py "Summarize the case for covered calls on FCX"
    python deepseek_flash.py --stream --no-thinking "Hello!"
"""
from __future__ import annotations

import argparse
import os
import sys

from openai import OpenAI

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-flash"


def make_client() -> OpenAI:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        sys.exit("Set the DEEPSEEK_API_KEY environment variable first.")
    return OpenAI(api_key=api_key, base_url=BASE_URL)


def _extra(thinking: bool, effort: str) -> dict:
    # DeepSeek-specific fields that the OpenAI SDK doesn't know about go in extra_body.
    if not thinking:
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled"}, "reasoning_effort": effort}


def ask(
    client: OpenAI,
    prompt: str,
    system: str = "You are a helpful assistant.",
    thinking: bool = True,
    effort: str = "high",
) -> tuple[str, str | None]:
    """Return (answer, reasoning). reasoning is None when thinking is off."""
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        stream=False,
        extra_body=_extra(thinking, effort),
    )
    msg = resp.choices[0].message
    reasoning = getattr(msg, "reasoning_content", None)
    if resp.usage:
        print(
            f"[tokens] prompt={resp.usage.prompt_tokens} "
            f"completion={resp.usage.completion_tokens} total={resp.usage.total_tokens}",
            file=sys.stderr,
        )
    return msg.content or "", reasoning


def ask_stream(
    client: OpenAI,
    prompt: str,
    system: str = "You are a helpful assistant.",
    thinking: bool = True,
    effort: str = "high",
) -> str:
    """Print the reply as it arrives (reasoning to stderr, answer to stdout)."""
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        stream=True,
        extra_body=_extra(thinking, effort),
    )
    answer = []
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            print(reasoning, end="", file=sys.stderr, flush=True)
        if delta.content:
            print(delta.content, end="", flush=True)
            answer.append(delta.content)
    print()
    return "".join(answer)


def main() -> None:
    p = argparse.ArgumentParser(description="Chat with deepseek-flash")
    p.add_argument("prompt", nargs="?", default="Hello!")
    p.add_argument("--stream", action="store_true", help="stream the reply")
    p.add_argument("--no-thinking", action="store_true", help="turn off reasoning mode")
    p.add_argument("--effort", default="high", choices=["low", "medium", "high"], help="reasoning effort")
    p.add_argument("--show-reasoning", action="store_true", help="print reasoning (non-stream mode)")
    args = p.parse_args()

    client = make_client()
    thinking = not args.no_thinking
    if args.stream:
        ask_stream(client, args.prompt, thinking=thinking, effort=args.effort)
        return

    answer, reasoning = ask(client, args.prompt, thinking=thinking, effort=args.effort)
    if args.show_reasoning and reasoning:
        print("--- reasoning ---\n" + reasoning + "\n--- answer ---")
    print(answer)


if __name__ == "__main__":
    main()
