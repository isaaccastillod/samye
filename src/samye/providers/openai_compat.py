"""OpenAI-compatible chat completions adapter."""

from __future__ import annotations

from samye.providers.base import HttpProvider, ProviderError


class OpenAICompatibleProvider(HttpProvider):
    """Complete text through an OpenAI-compatible chat endpoint."""

    async def complete(self, system: str, user: str) -> str:
        """Return the first assistant message's text content."""
        if self.cfg.base_url is None:
            raise ProviderError(f"{self.name}: base URL is not configured")
        headers: dict[str, str] = {}
        api_key = self.api_key(required=False)
        if api_key is not None:
            headers["Authorization"] = f"Bearer {api_key}"
        response = await self.post(
            f"{self.cfg.base_url.rstrip('/')}/v1/chat/completions",
            headers=headers,
            json={
                "model": self.cfg.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        data = self.response_json(response)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name}: malformed chat completion response: {data}") from exc
        return self.require_completion(content)
