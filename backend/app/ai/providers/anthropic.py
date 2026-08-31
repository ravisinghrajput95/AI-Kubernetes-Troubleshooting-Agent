"""Anthropic's Messages API.

Four differences from the OpenAI shape, and each one is a 400 or a silent
degradation if it is got wrong — which is the argument for two classes rather
than one with a vendor flag, since only a second implementation makes them
visible to a test:

- **`system` is a top-level parameter, not a message.** `PromptBuilder` emits
  OpenAI's shape, so `split_system` lifts it out. Leaving it in `messages` is
  rejected.
- **`max_tokens` is required.** OpenAI defaults it; here its absence is an
  error, and setting it too low truncates the diagnosis JSON mid-object, which
  surfaces as an unparseable response and a silent fall back to the
  deterministic path.
- **`temperature` is removed on current models** and returns a 400. The OpenAI
  provider sends `0.1`; sending it here would fail every request.
- **There is no `response_format: json_object`.** The prompt already specifies
  the JSON contract and `_parse_llm_json` strips code fences, so the contract is
  carried by the prompt rather than by the endpoint. That is a real difference
  in enforcement between the two providers, and it is why the evaluation corpus
  is the thing that decides whether a provider is usable, not this file.

Authentication is `x-api-key` with an `anthropic-version` header, not a bearer
token — copying the OpenAI headers is the mistake this comment exists to
prevent.
"""

from collections.abc import Sequence
from typing import Any

from app.ai.providers.base import Completion, HttpProvider, split_system

DEFAULT_BASE_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# Generous, because a truncated response is indistinguishable from a badly
# formed one by the time it reaches `_parse_llm_json`, and the diagnosis JSON is
# small enough that the ceiling never binds in practice.
DEFAULT_MAX_TOKENS = 16000


class AnthropicProvider(HttpProvider):
    name = "anthropic"

    def __init__(self, *args: Any, max_tokens: int = DEFAULT_MAX_TOKENS, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_tokens = max_tokens

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    def _body(self, messages: Sequence[dict[str, str]]) -> dict[str, Any]:
        system, conversation = split_system(messages)
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": conversation,
        }
        if system:
            body["system"] = system
        return body

    def _extract(self, payload: dict[str, Any]) -> Completion:
        """Read the text, and check why generation stopped before trusting it.

        A refusal arrives as **HTTP 200** with `stop_reason: "refusal"` and no
        usable content, so a provider that reads `content` without checking
        would hand an empty string to the JSON parser and report a parse failure
        — losing the one fact that explains it. `max_tokens` is the same shape:
        the content is real but truncated, and saying so is more useful than
        "unparseable".
        """
        stop_reason = payload.get("stop_reason")
        model = payload.get("model", self._model)

        if stop_reason == "refusal":
            details = payload.get("stop_details") or {}
            category = details.get("category") or "unspecified"
            return Completion(
                success=False,
                error=f"The model declined to answer (category: {category})",
                model=model,
                provider=self.name,
            )

        text = "".join(
            block.get("text", "")
            for block in payload.get("content") or []
            if block.get("type") == "text"
        )

        if stop_reason == "max_tokens":
            return Completion(
                success=False,
                error=(
                    f"The response hit the {self._max_tokens}-token ceiling and is "
                    f"truncated; raise LLM_MAX_TOKENS"
                ),
                model=model,
                provider=self.name,
            )

        if not text:
            return Completion(
                success=False,
                error=f"The response carried no text (stop_reason: {stop_reason})",
                model=model,
                provider=self.name,
            )

        return Completion(success=True, content=text, model=model, provider=self.name)
