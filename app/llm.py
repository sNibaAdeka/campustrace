"""One call path for text models, with a fallback provider.

Groq's free tier has per-minute *and* per-day token limits per model. When a
request is refused (429) or the provider is down, the same request goes to the
next configured OpenAI-compatible provider. Every result says which provider
and model answered, so the interface can name it.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

PROVIDERS = (
    # name, env key, endpoint, default model env, default model
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions", "GROQ_TEXT_MODEL", "openai/gpt-oss-20b"),
    ("cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1/chat/completions", "CEREBRAS_MODEL", "gpt-oss-120b"),
)


def configured() -> bool:
    return any(os.getenv(key) for _, key, *_ in PROVIDERS)


class LimitReached(Exception):
    def __init__(self, daily: bool):
        super().__init__("daily" if daily else "minute")
        self.daily = daily


async def chat(messages: list[dict[str, Any]], *, json_mode: bool = True, max_tokens: int = 700,
               groq_model: str | None = None, timeout: float = 20) -> tuple[str, str]:
    """Return (content, "provider:model"). Raises LimitReached if every
    configured provider refused for quota, httpx.HTTPError on other failures."""
    daily = False
    last_error: Exception | None = None
    attempts = []
    for name, key_env, url, model_env, default in PROVIDERS:
        if not os.getenv(key_env):
            continue
        models = [os.getenv(model_env, default)]
        if name == "groq":
            # Each Groq model has its own daily budget: try the preferred one,
            # then the other text models, before leaving the provider.
            models = list(dict.fromkeys([groq_model or models[0], models[0], "openai/gpt-oss-120b", "qwen/qwen3.8-27b"]))
        attempts += [(name, key_env, url, model) for model in models]
    for name, key_env, url, model in attempts:
        key = os.getenv(key_env)
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as exc:
            last_error = exc
            continue
        if response.status_code in (429, 413):
            daily = daily or "per day" in response.text or "TPD" in response.text
            continue
        if response.status_code >= 400:
            last_error = httpx.HTTPStatusError(f"{name} {response.status_code}", request=response.request, response=response)
            continue
        body = response.json()
        content = str(((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        return content, f"{name}:{model}"
    if last_error is not None and not daily:
        raise last_error
    raise LimitReached(daily)
