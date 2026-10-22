"""Load config.yaml and secrets from .env."""

import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

load_dotenv(ROOT / ".env")


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Copy config.example.yaml to config.yaml and fill it in.")
    with open(path) as f:
        return yaml.safe_load(f)


def school_short(school: str) -> str:
    """'Cornell University' -> 'Cornell'; 'University of Michigan' -> 'Michigan'."""
    short = re.sub(r"^(the\s+)?university of\s+|\s+university$|\s+college$", "", school.strip(), flags=re.I)
    return short or school


def project_path(relative: str) -> Path:
    """Resolve a config path relative to the project root."""
    return ROOT / relative
