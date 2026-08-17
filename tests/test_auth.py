"""Tests for Google OAuth credential acquisition."""

import stat
from pathlib import Path
from unittest.mock import Mock, patch

from google.auth.exceptions import RefreshError

from samye.auth import get_credentials
from samye.config import Config

SCOPES = ["scope-a", "scope-b"]


def make_config(tmp_path: Path) -> Config:
    return Config.model_validate(
        {
            "default_provider": "local",
            "providers": {
                "local": {
                    "type": "openai_compat",
                    "base_url": "http://localhost:11434",
                    "model": "model",
                }
            },
            "client_secret_path": tmp_path / "client.json",
            "token_path": tmp_path / "state" / "token.json",
        }
    )


def credentials(*, valid: bool, expired: bool = False, refresh_token: str | None = None) -> Mock:
    result = Mock()
    result.valid = valid
    result.expired = expired
    result.refresh_token = refresh_token
    result.to_json.return_value = '{"token":"redacted"}'
    return result


@patch("samye.auth.InstalledAppFlow")
@patch("samye.auth.Credentials.from_authorized_user_file")
def test_valid_cached_credentials_skip_flow(
    load: Mock, installed_flow: Mock, tmp_path: Path
) -> None:
    cfg = make_config(tmp_path)
    cfg.token_path.parent.mkdir()
    cfg.token_path.write_text("{}", encoding="utf-8")
    cached = credentials(valid=True)
    load.return_value = cached

    assert get_credentials(cfg, SCOPES) is cached
    load.assert_called_once_with(cfg.token_path, SCOPES)
    installed_flow.from_client_secrets_file.assert_not_called()


@patch("samye.auth.Request")
@patch("samye.auth.InstalledAppFlow")
@patch("samye.auth.Credentials.from_authorized_user_file")
def test_expired_credentials_refresh_and_are_saved_securely(
    load: Mock, installed_flow: Mock, request: Mock, tmp_path: Path
) -> None:
    cfg = make_config(tmp_path)
    cfg.token_path.parent.mkdir()
    cfg.token_path.write_text("{}", encoding="utf-8")
    cached = credentials(valid=False, expired=True, refresh_token="refresh")
    load.return_value = cached

    assert get_credentials(cfg, SCOPES) is cached
    cached.refresh.assert_called_once_with(request.return_value)
    installed_flow.from_client_secrets_file.assert_not_called()
    assert cfg.token_path.read_text(encoding="utf-8") == '{"token":"redacted"}\n'
    assert stat.S_IMODE(cfg.token_path.stat().st_mode) == 0o600
    assert not list(cfg.token_path.parent.glob(".token.json.*.tmp"))


@patch("samye.auth.InstalledAppFlow")
def test_corrupted_cache_runs_flow_and_replaces_token(
    installed_flow: Mock, tmp_path: Path
) -> None:
    cfg = make_config(tmp_path)
    cfg.token_path.parent.mkdir()
    cfg.token_path.write_text("not-json", encoding="utf-8")
    fresh = credentials(valid=True)
    installed_flow.from_client_secrets_file.return_value.run_local_server.return_value = fresh

    assert get_credentials(cfg, SCOPES) is fresh
    installed_flow.from_client_secrets_file.assert_called_once_with(
        str(cfg.client_secret_path), SCOPES
    )
    assert stat.S_IMODE(cfg.token_path.stat().st_mode) == 0o600


@patch("samye.auth.Request")
@patch("samye.auth.InstalledAppFlow")
@patch("samye.auth.Credentials.from_authorized_user_file")
def test_failed_refresh_runs_flow(
    load: Mock, installed_flow: Mock, request: Mock, tmp_path: Path
) -> None:
    cfg = make_config(tmp_path)
    cfg.token_path.parent.mkdir()
    cfg.token_path.write_text("{}", encoding="utf-8")
    cached = credentials(valid=False, expired=True, refresh_token="refresh")
    cached.refresh.side_effect = RefreshError("expired")
    load.return_value = cached
    fresh = credentials(valid=True)
    installed_flow.from_client_secrets_file.return_value.run_local_server.return_value = fresh

    assert get_credentials(cfg, SCOPES) is fresh
    cached.refresh.assert_called_once_with(request.return_value)
    installed_flow.from_client_secrets_file.assert_called_once()
