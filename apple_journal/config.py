"""Configuration and constants for the importer."""

from __future__ import annotations

import os
from pathlib import Path


PARSER_VERSION = "1.0.0"
PLAN_SCHEMA_VERSION = 1
DEFAULT_BANK = "default"
HINDSIGHT_ENV = "HINDSIGHT_MCP_URL"
DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_LEDGER_NAME = "ledger.json"
SOURCE_TAG = "source:apple-journal"
TYPE_TAG = "type:journal-entry"
REVIEW_THRESHOLD = 0.85
RETAIN_CONTEXT = "A first-person personal journal entry imported from Apple Journal. Preserve the writer's voice and the distinction between written text and structured attachment metadata."
HTTP_TIMEOUT = 60
POLL_INTERVAL = 1.0
POLL_GRACE_SECONDS = 30.0


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Load simple KEY=VALUE pairs without overriding process environment."""

    env_path = path or Path(".env")
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def hindsight_mcp_url(dotenv_path: Path | None = None) -> str:
    values = load_dotenv(dotenv_path)
    return os.environ.get(HINDSIGHT_ENV) or values.get(HINDSIGHT_ENV, "")
