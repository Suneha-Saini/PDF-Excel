"""Generic 'system prompt + user prompt -> JSON object' call for any configured AI provider.

The existing providers only expose `extract()` (bank-statement prompt). Ledger coding needs a
different prompt, so this dispatches on the provider type using the same credentials/models.
The model must be asked to return a JSON *object* (not a bare list).
"""
import logging
from typing import Any, Dict

import httpx

from app.ai.manager import AIProvider, AIProviderException
from app.ai.local_provider import LocalProvider
from app.ai.cloud_providers import (
    OpenAIProvider, ClaudeProvider, GeminiProvider, OpenRouterProvider, GroqProvider,
)

logger = logging.getLogger("app.ai.generic")

_OPENAI_STYLE = {
    OpenAIProvider: ("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    OpenRouterProvider: ("https://openrouter.ai/api/v1/chat/completions", "google/gemini-2.5-flash"),
    GroqProvider: ("https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b"),
}


def complete_json(provider: AIProvider, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
    try:
        cls = type(provider)

        if cls in _OPENAI_STYLE:
            url, default_model = _OPENAI_STYLE[cls]
            if not provider.api_key:
                raise AIProviderException(f"{cls.__name__.replace('Provider', '')} API key is not configured.")
            payload = {
                "model": provider.model_name or default_model,
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
            }
            with httpx.Client(timeout=120.0) as c:
                r = c.post(url, headers={"Authorization": f"Bearer {provider.api_key}"}, json=payload)
                r.raise_for_status()
                return provider.clean_json_response(r.json()["choices"][0]["message"]["content"])

        if cls is ClaudeProvider:
            if not provider.api_key:
                raise AIProviderException("Anthropic API key is not configured.")
            payload = {
                "model": provider.model_name or "claude-3-5-sonnet-20240620",
                "max_tokens": 4000, "temperature": 0.0, "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
            with httpx.Client(timeout=120.0) as c:
                r = c.post("https://api.anthropic.com/v1/messages",
                           headers={"x-api-key": provider.api_key, "anthropic-version": "2023-06-01"}, json=payload)
                r.raise_for_status()
                return provider.clean_json_response(r.json()["content"][0]["text"])

        if cls is GeminiProvider:
            if not provider.api_key:
                raise AIProviderException("Gemini API key is not configured.")
            model = provider.model_name or "gemini-2.5-flash"
            payload = {
                "contents": [{"parts": [{"text": f"{system_prompt}\n\n{user_prompt}"}]}],
                "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
            }
            with httpx.Client(timeout=120.0) as c:
                r = c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                           headers={"x-goog-api-key": provider.api_key}, json=payload)  # header keeps the key out of URLs/logs
                r.raise_for_status()
                return provider.clean_json_response(r.json()["candidates"][0]["content"]["parts"][0]["text"])

        if cls is LocalProvider:
            base = provider.base_url or ""
            is_ollama = "11434" in base or "ollama" in base.lower() or not base
            with httpx.Client(timeout=300.0) as c:
                if is_ollama:
                    r = c.post(f"{base or 'http://localhost:11434'}/api/chat", json={
                        "model": provider.model_name, "stream": False, "format": "json",
                        "options": {"temperature": 0.0},
                        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                    })
                    r.raise_for_status()
                    text = r.json().get("message", {}).get("content", "")
                else:
                    r = c.post(f"{base or 'http://localhost:1234'}/v1/chat/completions", json={
                        "model": provider.model_name, "temperature": 0.0,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                    })
                    r.raise_for_status()
                    text = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            if not text:
                raise AIProviderException("Local AI response was empty.")
            return provider.clean_json_response(text)

        raise AIProviderException(f"Unsupported provider type: {cls.__name__}")
    except AIProviderException:
        raise
    except httpx.HTTPStatusError as exc:
        # Include the provider's own explanation (e.g. "model not found") instead of just "400 Bad Request".
        body = exc.response.text[:300].replace("\n", " ")
        logger.error(f"AI call failed: HTTP {exc.response.status_code}: {body}")
        raise AIProviderException(f"HTTP {exc.response.status_code} from the AI provider: {body}")
    except Exception as exc:
        logger.error(f"AI call failed: {exc}")
        raise AIProviderException(f"AI request failed: {exc}")
