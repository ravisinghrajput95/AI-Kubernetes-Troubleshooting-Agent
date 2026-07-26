import json
import time
from typing import Any

import httpx
from loguru import logger

from app.core.config import settings


class LLMClient:
    def __init__(self) -> None:
        self.base_url = "https://api.openai.com/v1/chat/completions"

    def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        if not settings.openai_api_key:
            logger.warning("OPENAI_API_KEY is not configured")
            return {
                "success": False,
                "error": "OPENAI_API_KEY is not configured",
                "content": "",
            }

        payload = {
            "model": settings.openai_model,
            "messages": messages,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }

        last_error = ""
        for attempt in range(1, 4):
            try:
                with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
                    response = client.post(self.base_url, headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    content = data["choices"][0]["message"]["content"]
                    return {"success": True, "error": "", "content": content}
            except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as exc:
                last_error = str(exc)
                logger.error(
                    "OpenAI request failed on attempt {attempt}: {error}",
                    attempt=attempt,
                    error=last_error,
                )
                time.sleep(0.5 * attempt)

        return {"success": False, "error": last_error, "content": ""}
