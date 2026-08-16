"""Anthropic Messages API adapter."""

from __future__ import annotations

from samye.providers.base import HttpProvider, ProviderError

DEFAULT_BASE_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicProvider(HttpProvider):
    """Complete text through Anthropic's Messages API."""

    async def complete(self, system: str, user: str) -> str:
        """Return all text blocks from the first Messages response."""
        api_key = self.api_key(required=True)
        base_url = (self.cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        response = await self.post(
            f"{base_url}/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
            json={
                "model": self.cfg.model,
                "max_tokens": 4096,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        data = self.response_json(response)
        try:
            blocks = data["content"]
            text = "".join(
                block["text"]
                for block in blocks
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
        except (KeyError, TypeError) as exc:
            raise ProviderError(f"{self.name}: malformed Messages response: {data}") from exc
        return self.require_completion(text)
