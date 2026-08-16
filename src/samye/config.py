"""Configuration models and TOML loading."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pydantic


class ProviderCfg(pydantic.BaseModel):
    """Configuration for one completion provider."""

    type: Literal["openai_compat", "anthropic", "gemini"]
    base_url: str | None = None
    model: str
    api_key_env: str | None = None
    timeout_s: float = 120.0


class Config(pydantic.BaseModel):
    """Top-level samye configuration."""

    poll_interval_s: float = 5.0
    context_chars: int = 2000
    max_docs: int = 25
    default_provider: str
    providers: dict[str, ProviderCfg]
    docs: list[str] = []
    client_secret_path: Path = Path("~/.config/samye/client_secret.json")
    token_path: Path = Path("~/.local/state/samye/token.json")


def load_config(path: Path | None = None) -> Config:
    """Load and validate samye configuration from TOML."""
    raise NotImplementedError
