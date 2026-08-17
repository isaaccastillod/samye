"""Google OAuth credential acquisition and token persistence."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from samye.config import Config

LOGGER = logging.getLogger(__name__)


def _save_credentials(credentials: Credentials, path: Path) -> None:
    """Atomically persist credentials without exposing them to other users."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(credentials.to_json())
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def get_credentials(cfg: Config, scopes: list[str]) -> Credentials:
    """Load, refresh, or interactively acquire Google credentials.

    Interactive authorization needs a browser. For a headless host, run authorization
    once on a browser-equipped machine and copy or mount the resulting token file at
    ``cfg.token_path``.
    """
    token_path = cfg.token_path.expanduser()
    credentials: Credentials | None = None
    if token_path.exists():
        try:
            credentials = Credentials.from_authorized_user_file(token_path, scopes)
        except (OSError, UnicodeError, ValueError) as exc:
            LOGGER.warning("ignoring an unreadable OAuth token cache: %s", type(exc).__name__)

    if credentials is not None and credentials.valid:
        return credentials

    if credentials is not None and credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except RefreshError:
            LOGGER.warning("OAuth token refresh failed; starting authorization again")
        else:
            _save_credentials(credentials, token_path)
            return credentials

    flow = InstalledAppFlow.from_client_secrets_file(
        str(cfg.client_secret_path.expanduser()),
        scopes,
    )
    credentials = flow.run_local_server(port=0)
    _save_credentials(credentials, token_path)
    return credentials
