"""Tests run against the committed example config/template, never a user's personal files."""

from referral import compose, config

config.load_config.__defaults__ = (config.ROOT / "config.example.yaml",)
compose.load_template.__defaults__ = (compose.EXAMPLE_TEMPLATE_PATH,)
