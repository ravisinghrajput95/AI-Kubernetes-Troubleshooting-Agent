"""OpenAI chat completions, and anything that speaks the same wire format.

The behaviour here is what `LLMClient` did before providers existed, moved
rather than rewritten — the endpoint, the 0.1 temperature, the
`response_format: json_object` and the three linear-backoff attempts are all
unchanged, so an existing deployment sees no difference.

`OpenAICompatibleProvider` is the same request against a different base URL.
That covers Ollama, vLLM, LiteLLM, OpenRouter and the various Bedrock and Azure
gateways, all of which chose to implement this shape — which is why it is worth
a class of its own rather than a flag: a deployment that cannot send its
cluster's interior to a third party can run a model on its own hardware without
the platform learning a fourth wire format.
"""

from collections.abc import Sequence
from typing import Any

from app.ai.providers.base import Completion, HttpProvider

DEFAULT_BASE_URL = "https://api.openai.com/v1/chat/completions"


class OpenAIProvider(HttpProvider):
    name = "openai"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _body(self, messages: Sequence[dict[str, str]]) -> dict[str, Any]:
        return {
            "model": self._model,
            "messages": [dict(message) for message in messages],
            "temperature": 0.1,
            # The reasoning contract is a JSON object with named fields, and
            # `_parse_llm_json` rejects anything else. Asking the endpoint to
            # enforce it removes one class of fallback.
            "response_format": {"type": "json_object"},
        }

    def _extract(self, payload: dict[str, Any]) -> Completion:
        content = payload["choices"][0]["message"]["content"]
        return Completion(
            success=True,
            content=content,
            model=payload.get("model", self._model),
            provider=self.name,
        )


class OpenAICompatibleProvider(OpenAIProvider):
    """The same request, pointed at something you run.

    Named separately from `openai` so `investigation["diagnosis"]` and the logs
    can say which one answered. A local model and OpenAI producing the same
    output is a claim worth being able to check, and a single provider name
    covering both would make it uncheckable.

    **The API key is optional here and required there.** A vLLM or Ollama
    endpoint on a private network commonly has no auth at all, so requiring one
    would mean inventing a placeholder — which is how a real credential ends up
    hardcoded as `"none"` in somebody's values file.
    """

    name = "compatible"

    @property
    def configured(self) -> bool:
        return bool(self._base_url)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers
