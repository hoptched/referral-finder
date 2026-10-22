"""Tests run against the committed example config/template and fake API keys, never a user's
personal files or real keys (so they pass the same on a fresh fork with no .env)."""

import os

# Set before referral is imported: load_dotenv() doesn't override variables that already exist.
for key in ("ANTHROPIC_API_KEY", "HUNTER_API_KEY"):
    os.environ[key] = "test-key"
for key in ("APOLLO_API_KEY", "ANTHROPIC_WORKSPACE_ID"):
    os.environ[key] = ""

from referral import compose, config  # noqa: E402

config.load_config.__defaults__ = (config.ROOT / "config.example.yaml",)
compose.load_template.__defaults__ = (compose.EXAMPLE_TEMPLATE_PATH,)
