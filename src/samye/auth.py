"""Google OAuth credential acquisition and token persistence."""

from __future__ import annotations

from google.oauth2.credentials import Credentials

from samye.config import Config


def get_credentials(cfg: Config, scopes: list[str]) -> Credentials:
    """Load, refresh, or interactively acquire Google credentials."""
    raise NotImplementedError
