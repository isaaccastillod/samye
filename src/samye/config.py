"""Configuration models and TOML loading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import pydantic


class ConfigError(ValueError):
    """A configuration file could not be loaded or validated."""


class ProviderCfg(pydantic.BaseModel):
    """Configuration for one completion provider."""

    model_config = pydantic.ConfigDict(extra="forbid", validate_default=True)

    type: Literal["openai_compat", "anthropic", "gemini"]
    base_url: str | None = None
    model: str
    api_key_env: str | None = None
    timeout_s: float = 120.0

    @pydantic.field_validator("model", "api_key_env")
    @classmethod
    def nonempty_strings(cls, value: str | None) -> str | None:
        """Reject empty model and environment-variable names."""
        if value is not None:
            value = value.strip()
            if not value:
                raise ValueError("must not be empty")
        return value

    @pydantic.field_validator("timeout_s")
    @classmethod
    def positive_timeout(cls, value: float) -> float:
        """Require a useful provider timeout."""
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @pydantic.model_validator(mode="after")
    def validate_provider(self) -> ProviderCfg:
        """Validate fields whose requirements depend on provider type."""
        if self.type == "openai_compat":
            if self.base_url is None or not self.base_url.strip():
                raise ValueError("base_url is required for openai_compat providers")
            base_url = self.base_url.strip().rstrip("/")
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("base_url must be an absolute HTTP(S) URL")
            self.base_url = base_url
        elif self.api_key_env is None:
            raise ValueError(f"api_key_env is required for {self.type} providers")
        return self


class Config(pydantic.BaseModel):
    """Top-level samye configuration."""

    model_config = pydantic.ConfigDict(extra="forbid", validate_default=True)

    write_mode: Literal["propose", "reply", "suggest"] = "propose"
    web_port: int = 8321
    web_bind_host: str = "127.0.0.1"
    web_base_url: str | None = None
    poll_interval_s: float = 5.0
    context_chars: int = 2000
    max_docs: int = 25
    default_provider: str
    providers: dict[str, ProviderCfg]
    docs: list[str] = pydantic.Field(default_factory=list)
    client_secret_path: Path = Path("~/.config/samye/client_secret.json")
    token_path: Path = Path("~/.local/state/samye/token.json")

    @pydantic.field_validator("web_port")
    @classmethod
    def valid_web_port(cls, value: int) -> int:
        """Require a valid TCP port."""
        if not 1 <= value <= 65535:
            raise ValueError("must be between 1 and 65535")
        return value

    @pydantic.field_validator("web_base_url")
    @classmethod
    def absolute_web_base_url(cls, value: str | None) -> str | None:
        """Require an absolute HTTP(S) URL when the web UI is advertised."""
        if value is None:
            return None
        value = value.strip().rstrip("/")
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("must be an absolute HTTP(S) URL")
        return value

    @pydantic.field_validator("poll_interval_s")
    @classmethod
    def positive_poll_interval(cls, value: float) -> float:
        """Require a positive polling interval."""
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @pydantic.field_validator("context_chars")
    @classmethod
    def nonnegative_context(cls, value: int) -> int:
        """Allow disabling context while rejecting nonsensical values."""
        if value < 0:
            raise ValueError("must be zero or greater")
        return value

    @pydantic.field_validator("max_docs")
    @classmethod
    def positive_doc_limit(cls, value: int) -> int:
        """Require at least one document slot."""
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value

    @pydantic.field_validator("default_provider")
    @classmethod
    def nonempty_default_provider(cls, value: str) -> str:
        """Reject an empty default provider name."""
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @pydantic.field_validator("providers")
    @classmethod
    def validate_provider_names(cls, value: dict[str, ProviderCfg]) -> dict[str, ProviderCfg]:
        """Require at least one provider with a nonempty name."""
        if not value:
            raise ValueError("must define at least one provider")
        if any(not name.strip() for name in value):
            raise ValueError("provider names must not be empty")
        return value

    @pydantic.field_validator("docs")
    @classmethod
    def validate_docs(cls, value: list[str]) -> list[str]:
        """Reject blank and duplicate configured document IDs."""
        docs = [doc.strip() for doc in value]
        if any(not doc for doc in docs):
            raise ValueError("document IDs must not be empty")
        if len(docs) != len(set(docs)):
            raise ValueError("document IDs must be unique")
        return docs

    @pydantic.field_validator("client_secret_path", "token_path")
    @classmethod
    def expand_paths(cls, value: Path) -> Path:
        """Expand user-home markers in filesystem paths."""
        return value.expanduser()

    @pydantic.model_validator(mode="after")
    def validate_default_provider(self) -> Config:
        """Require the default provider to name a configured provider."""
        if self.default_provider not in self.providers:
            raise ValueError(
                f"default_provider {self.default_provider!r} is not present in providers"
            )
        return self


def load_config(path: Path | None = None) -> Config:
    """Load and validate samye configuration from TOML."""
    config_path = (path or Path("~/.config/samye/config.toml")).expanduser()
    try:
        contents = config_path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {config_path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {config_path}: {exc}") from exc

    try:
        data = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    try:
        return Config.model_validate(data)
    except pydantic.ValidationError as exc:
        raise ConfigError(f"invalid configuration in {config_path}:\n{exc}") from exc
