"""Common completion provider protocol and factory."""

from __future__ import annotations

import asyncio
import math
import os
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from samye.config import ProviderCfg

SERVER_RETRY_DELAYS = (0.25, 0.5)
DEFAULT_RATE_LIMIT_DELAY = 1.0
Sleep = Callable[[float], Awaitable[None]]


class ProviderError(Exception):
    """A provider could not return a usable completion."""


class Provider(Protocol):
    """A named asynchronous text completion provider."""

    name: str

    async def complete(self, system: str, user: str) -> str:
        """Return replacement text or raise ProviderError."""
        ...


class HttpProvider:
    """Shared HTTP, authentication, and retry behavior for provider adapters."""

    def __init__(
        self,
        name: str,
        cfg: ProviderCfg,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.name = name
        self.cfg = cfg
        self._client = client
        self._sleep = sleep

    def api_key(self, *, required: bool) -> str | None:
        """Read an API key from its configured environment variable."""
        env_name = self.cfg.api_key_env
        if env_name is None:
            if required:
                raise ProviderError(f"{self.name}: API key environment variable is not configured")
            return None
        value = os.environ.get(env_name)
        if value is None or not value.strip():
            raise ProviderError(f"{self.name}: environment variable {env_name} is not set")
        return value

    async def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, object],
    ) -> httpx.Response:
        """POST JSON with the pinned retry policy."""
        if self._client is not None:
            return await self._post_with_client(self._client, url, headers=headers, json=json)
        async with httpx.AsyncClient(timeout=self.cfg.timeout_s) as client:
            return await self._post_with_client(client, url, headers=headers, json=json)

    async def _post_with_client(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: Mapping[str, str] | None,
        json: Mapping[str, object],
    ) -> httpx.Response:
        server_retries = 0
        rate_limit_retried = False
        while True:
            try:
                response = await client.post(url, headers=headers, json=json)
            except httpx.TimeoutException as exc:
                if server_retries == len(SERVER_RETRY_DELAYS):
                    raise ProviderError(
                        f"{self.name}: request timed out after retries: {exc}"
                    ) from exc
                await self._sleep(SERVER_RETRY_DELAYS[server_retries])
                server_retries += 1
                continue
            except httpx.HTTPError as exc:
                raise ProviderError(f"{self.name}: request failed: {exc}") from exc

            if response.status_code == 429:
                if rate_limit_retried:
                    raise self._response_error(response)
                rate_limit_retried = True
                await self._sleep(_retry_after_seconds(response.headers.get("Retry-After")))
                continue

            if 500 <= response.status_code < 600:
                if server_retries == len(SERVER_RETRY_DELAYS):
                    raise self._response_error(response)
                await self._sleep(SERVER_RETRY_DELAYS[server_retries])
                server_retries += 1
                continue

            if not 200 <= response.status_code < 300:
                raise self._response_error(response)
            return response

    def response_json(self, response: httpx.Response) -> dict[str, Any]:
        """Decode an object response while retaining diagnostic detail internally."""
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"{self.name}: provider returned invalid JSON: {response.text}"
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(f"{self.name}: provider returned a non-object JSON response")
        return data

    def require_completion(self, value: object) -> str:
        """Require text without altering it; empty text represents a deletion."""
        if not isinstance(value, str):
            raise ProviderError(f"{self.name}: provider returned a non-text completion")
        return value

    def _response_error(self, response: httpx.Response) -> ProviderError:
        return ProviderError(
            f"{self.name}: provider returned HTTP {response.status_code}: {response.text}"
        )


def _retry_after_seconds(value: str | None) -> float:
    if value is None:
        return DEFAULT_RATE_LIMIT_DELAY
    try:
        delay = float(value)
        if math.isfinite(delay):
            return max(0.0, delay)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_RATE_LIMIT_DELAY


def make_provider(name: str, cfg: ProviderCfg) -> Provider:
    """Construct the configured provider adapter."""
    if cfg.type == "openai_compat":
        from samye.providers.openai_compat import OpenAICompatibleProvider

        return OpenAICompatibleProvider(name, cfg)
    if cfg.type == "anthropic":
        from samye.providers.anthropic import AnthropicProvider

        return AnthropicProvider(name, cfg)
    if cfg.type == "gemini":
        from samye.providers.gemini import GeminiProvider

        return GeminiProvider(name, cfg)
    raise ValueError(f"unsupported provider type: {cfg.type}")
