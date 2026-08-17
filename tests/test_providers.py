"""Mocked-transport tests for completion provider adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

import httpx
import pytest

from samye.config import ProviderCfg
from samye.providers.anthropic import AnthropicProvider
from samye.providers.base import Provider, ProviderError, make_provider
from samye.providers.gemini import GeminiProvider
from samye.providers.openai_compat import OpenAICompatibleProvider

ProviderType = Literal["openai_compat", "anthropic", "gemini"]
PROVIDER_TYPES: tuple[ProviderType, ...] = ("openai_compat", "anthropic", "gemini")


@dataclass
class SleepRecorder:
    delays: list[float] = field(default_factory=list)

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def provider_config(provider_type: ProviderType) -> ProviderCfg:
    if provider_type == "openai_compat":
        return ProviderCfg(
            type="openai_compat",
            base_url="http://localhost:11434",
            model="test-model",
            timeout_s=2,
        )
    return ProviderCfg(
        type=provider_type,
        model="test-model",
        api_key_env="SAMYE_TEST_API_KEY",
        timeout_s=2,
    )


def adapter(
    provider_type: ProviderType,
    client: httpx.AsyncClient,
    sleep: SleepRecorder,
) -> Provider:
    cfg = provider_config(provider_type)
    if provider_type == "openai_compat":
        return OpenAICompatibleProvider("configured-name", cfg, client=client, sleep=sleep)
    if provider_type == "anthropic":
        return AnthropicProvider("configured-name", cfg, client=client, sleep=sleep)
    return GeminiProvider("configured-name", cfg, client=client, sleep=sleep)


def completion_response(provider_type: ProviderType, text: str) -> dict[str, object]:
    if provider_type == "openai_compat":
        return {"choices": [{"message": {"content": text}}]}
    if provider_type == "anthropic":
        return {"content": [{"type": "text", "text": text}]}
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


@pytest.mark.parametrize(
    ("provider_type", "expected_url"),
    [
        ("openai_compat", "http://localhost:11434/v1/chat/completions"),
        ("anthropic", "https://api.anthropic.com/v1/messages"),
        (
            "gemini",
            "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent",
        ),
    ],
)
async def test_success_request_and_response_shapes(
    provider_type: ProviderType,
    expected_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAMYE_TEST_API_KEY", "test-secret")

    def handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == expected_url
        body = json.loads(request.content)
        if provider_type == "openai_compat":
            assert "authorization" not in request.headers
            assert body == {
                "model": "test-model",
                "messages": [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "user prompt"},
                ],
            }
        elif provider_type == "anthropic":
            assert request.headers["x-api-key"] == "test-secret"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert body == {
                "model": "test-model",
                "max_tokens": 4096,
                "system": "system prompt",
                "messages": [{"role": "user", "content": "user prompt"}],
            }
        else:
            assert request.headers["x-goog-api-key"] == "test-secret"
            assert body == {
                "systemInstruction": {"parts": [{"text": "system prompt"}]},
                "contents": [{"role": "user", "parts": [{"text": "user prompt"}]}],
            }
        return httpx.Response(200, json=completion_response(provider_type, "replacement"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = adapter(provider_type, client, SleepRecorder())
        result = await provider.complete("system prompt", "user prompt")

    assert result == "replacement"
    assert provider.name == "configured-name"


@pytest.mark.parametrize("provider_type", PROVIDER_TYPES)
async def test_retries_5xx_twice(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMYE_TEST_API_KEY", "test-secret")
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, json=completion_response(provider_type, "recovered"))

    sleep = SleepRecorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await adapter(provider_type, client, sleep).complete("system", "user")

    assert result == "recovered"
    assert attempts == 3
    assert sleep.delays == [0.25, 0.5]


@pytest.mark.parametrize("provider_type", PROVIDER_TYPES)
async def test_retries_timeouts_twice(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMYE_TEST_API_KEY", "test-secret")
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ReadTimeout("too slow", request=request)
        return httpx.Response(200, json=completion_response(provider_type, "recovered"))

    sleep = SleepRecorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await adapter(provider_type, client, sleep).complete("system", "user")

    assert result == "recovered"
    assert attempts == 3
    assert sleep.delays == [0.25, 0.5]


@pytest.mark.parametrize("provider_type", PROVIDER_TYPES)
async def test_retries_429_once_using_retry_after(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMYE_TEST_API_KEY", "test-secret")
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "1.75"}, text="rate limited")
        return httpx.Response(200, json=completion_response(provider_type, "recovered"))

    sleep = SleepRecorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await adapter(provider_type, client, sleep).complete("system", "user")

    assert result == "recovered"
    assert attempts == 2
    assert sleep.delays == [1.75]


@pytest.mark.parametrize("provider_type", PROVIDER_TYPES)
async def test_accepts_empty_completion_as_deletion(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMYE_TEST_API_KEY", "test-secret")

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion_response(provider_type, ""))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await adapter(provider_type, client, SleepRecorder()).complete("system", "user")

    assert result == ""


async def test_openai_compat_rejects_non_text_completion() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        provider = OpenAICompatibleProvider(
            "configured-name",
            provider_config("openai_compat"),
            client=client,
        )
        with pytest.raises(ProviderError, match="non-text completion"):
            await provider.complete("system", "user")


@pytest.mark.parametrize("provider_type", PROVIDER_TYPES)
async def test_second_429_fails_without_another_retry(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SAMYE_TEST_API_KEY", "test-secret")
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, headers={"Retry-After": "0"}, text="still limited")

    sleep = SleepRecorder()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError, match="still limited"):
            await adapter(provider_type, client, sleep).complete("system", "user")

    assert attempts == 2
    assert sleep.delays == [0.0]


@pytest.mark.parametrize(
    ("provider_type", "expected_class"),
    [
        ("openai_compat", OpenAICompatibleProvider),
        ("anthropic", AnthropicProvider),
        ("gemini", GeminiProvider),
    ],
)
def test_factory_dispatches(
    provider_type: ProviderType,
    expected_class: type[OpenAICompatibleProvider | AnthropicProvider | GeminiProvider],
) -> None:
    assert isinstance(make_provider("alias", provider_config(provider_type)), expected_class)


@pytest.mark.parametrize("provider_type", ["anthropic", "gemini"])
async def test_hosted_provider_requires_api_key_at_runtime(
    provider_type: ProviderType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SAMYE_TEST_API_KEY", raising=False)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200))
    ) as client:
        with pytest.raises(ProviderError, match="SAMYE_TEST_API_KEY is not set"):
            await adapter(provider_type, client, SleepRecorder()).complete("system", "user")
