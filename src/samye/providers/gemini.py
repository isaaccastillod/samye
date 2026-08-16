"""Google Gemini generateContent adapter."""

from __future__ import annotations

from urllib.parse import quote

from samye.providers.base import HttpProvider, ProviderError

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiProvider(HttpProvider):
    """Complete text through Gemini's generateContent endpoint."""

    async def complete(self, system: str, user: str) -> str:
        """Return all text parts from the first Gemini candidate."""
        api_key = self.api_key(required=True)
        base_url = (self.cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        model = self.cfg.model.removeprefix("models/")
        response = await self.post(
            f"{base_url}/models/{quote(model, safe='')}:generateContent",
            headers={"x-goog-api-key": api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
            },
        )
        data = self.response_json(response)
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(
                part["text"]
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name}: malformed generateContent response: {data}") from exc
        return self.require_completion(text)
