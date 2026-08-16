"""Tests for configuration loading and validation."""

from pathlib import Path

import pytest

from samye.config import ConfigError, load_config


def write_config(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_loads_local_provider_and_expands_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    path = write_config(
        tmp_path / "config.toml",
        """
default_provider = "local"
docs = ["doc-1", "doc-2"]

[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434/"
model = "gpt-oss:120b"
""",
    )

    config = load_config(path)

    assert config.default_provider == "local"
    assert config.providers["local"].base_url == "http://localhost:11434"
    assert config.client_secret_path == tmp_path / ".config/samye/client_secret.json"
    assert config.token_path == tmp_path / ".local/state/samye/token.json"
    assert config.docs == ["doc-1", "doc-2"]


def test_loads_hosted_providers(tmp_path: Path) -> None:
    path = write_config(
        tmp_path / "config.toml",
        """
default_provider = "anthropic"

[providers.anthropic]
type = "anthropic"
model = "claude"
api_key_env = "ANTHROPIC_API_KEY"

[providers.gemini]
type = "gemini"
model = "gemini"
api_key_env = "GEMINI_API_KEY"
""",
    )

    config = load_config(path)

    assert set(config.providers) == {"anthropic", "gemini"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("providers = {}", "default_provider"),
        (
            """
default_provider = "missing"
[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434"
model = "model"
""",
            "is not present in providers",
        ),
        (
            """
default_provider = "local"
[providers.local]
type = "openai_compat"
model = "model"
""",
            "base_url is required",
        ),
        (
            """
default_provider = "local"
[providers.local]
type = "openai_compat"
base_url = "localhost:11434"
model = "model"
""",
            r"absolute HTTP\(S\) URL",
        ),
        (
            """
default_provider = "local"
[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434"
model = ""
""",
            "must not be empty",
        ),
        (
            """
default_provider = "local"
[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434"
model = "model"
timeout_s = 0
""",
            "must be greater than zero",
        ),
        (
            """
default_provider = "anthropic"
[providers.anthropic]
type = "anthropic"
model = "model"
""",
            "api_key_env is required",
        ),
        (
            """
default_provider = "anthropic"
[providers.anthropic]
type = "anthropic"
model = "model"
api_key_env = " "
""",
            "must not be empty",
        ),
        (
            """
default_provider = "gemini"
[providers.gemini]
type = "gemini"
model = "model"
""",
            "api_key_env is required",
        ),
        (
            """
default_provider = "local"
poll_interval_s = 0
[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434"
model = "model"
""",
            "must be greater than zero",
        ),
        (
            """
default_provider = "local"
context_chars = -1
[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434"
model = "model"
""",
            "must be zero or greater",
        ),
        (
            """
default_provider = "local"
max_docs = 0
[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434"
model = "model"
""",
            "must be greater than zero",
        ),
        (
            """
default_provider = "local"
unknown = true
[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434"
model = "model"
""",
            "Extra inputs are not permitted",
        ),
        (
            """
default_provider = "local"
docs = ["same", "same"]
[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434"
model = "model"
""",
            "document IDs must be unique",
        ),
        (
            """
default_provider = "local"
docs = [""]
[providers.local]
type = "openai_compat"
base_url = "http://localhost:11434"
model = "model"
""",
            "document IDs must not be empty",
        ),
    ],
)
def test_rejects_invalid_config(tmp_path: Path, body: str, message: str) -> None:
    path = write_config(tmp_path / "config.toml", body)

    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_reports_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "missing.toml"

    with pytest.raises(ConfigError, match=f"configuration file not found: {path}"):
        load_config(path)


def test_reports_invalid_toml(tmp_path: Path) -> None:
    path = write_config(tmp_path / "config.toml", "not = [valid")

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)
