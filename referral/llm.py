"""Shared Anthropic client."""

import json
import os
from datetime import datetime

import anthropic

from referral import config  # noqa: F401  (loads .env)
from referral.config import project_path

USAGE_LOG = project_path(".data/claude_usage.jsonl")
# USD per million tokens (input, output), standard API rates.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}


def client() -> anthropic.AsyncAnthropic:
    # The key isn't workspace-scoped, so every request must name the workspace.
    headers = {}
    if workspace := os.environ.get("ANTHROPIC_WORKSPACE_ID"):
        headers["anthropic-workspace-id"] = workspace
    return anthropic.AsyncAnthropic(default_headers=headers)


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES.get(model, (0.0, 0.0))
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def log_usage(task: str, response) -> None:
    """Append one Claude call's token usage (and its cost) to .data/claude_usage.jsonl."""
    usage = response.usage
    # Thinking tokens are billed as output and are included in output_tokens.
    entry = {"at": datetime.now().isoformat(), "task": task, "model": response.model,
             "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
    entry["usd"] = round(cost(response.model, entry["input_tokens"], entry["output_tokens"]), 6)
    USAGE_LOG.parent.mkdir(exist_ok=True)
    with USAGE_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def usage_entries() -> list[dict]:
    try:
        return [json.loads(line) for line in USAGE_LOG.read_text().splitlines() if line.strip()]
    except FileNotFoundError:
        return []
