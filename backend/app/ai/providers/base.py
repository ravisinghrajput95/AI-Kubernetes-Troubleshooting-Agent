"""The seam between the reasoning layer and whatever model answers it.

`docs/PRODUCTION_READINESS.md` carried "no provider abstraction" against F11 for
several milestones, and the shape of that gap was specific: `LLMClient` posted to
`api.openai.com` with the request body inlined, so choosing a different model
meant editing the class that talks to the network. A deployment that could not
send its cluster's interior to OpenAI had no option at all.

**Written against `httpx` rather than a vendor SDK**, which is the same decision
this repository already made twice — the MCP JSON-RPC subset is hand-written, and
`axios` was removed from the console for costing more than the console's own
code. The cost is stated rather than implied: new API features do not arrive for
free, so the supported request surface is the one named in each provider.

Three implementations, because an abstraction with one is a redirection. They
differ in ways a test can see: OpenAI takes a `system` *message*, Anthropic takes
a top-level `system` **parameter** and rejects `temperature` outright, and the
compatible provider is the same wire format as OpenAI pointed somewhere else.
"""

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx
from loguru import logger

# Attempts, and the linear backoff between them. Carried over from the original
# client unchanged: a reasoning call is on the investigation's critical path and
# a failure degrades to the deterministic diagnosis rather than failing the run,
# so waiting longer buys less here than it would elsewhere.
ATTEMPTS = 3
BACKOFF_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class Completion:
    """What a provider returns, whatever it talks to.

    A failure is a value rather than an exception, because the caller's response
    to one is not to stop: `RootCauseAnalyzer` falls back to the deterministic
    diagnosis and records *why* in the investigation. An exception would have to
    be caught and turned back into this at the one call site.
    """

    success: bool
    content: str = ""
    error: str = ""
    model: str = ""
    provider: str = ""


@runtime_checkable
class LLMProvider(Protocol):
    """One model endpoint, reduced to the only question the platform asks it."""

    name: str

    @property
    def configured(self) -> bool:
        """False when no credential is set, so the caller can skip the call."""
        ...

    def complete(self, messages: Sequence[dict[str, str]]) -> Completion: ...


class HttpProvider:
    """Shared retry, timeout and error handling for an HTTP-speaking provider.

    Subclasses describe *what* to send and *how to read the answer*; nothing
    below the `_request` / `_extract` pair differs between vendors, and keeping
    it here is what stops one provider quietly acquiring different retry
    behaviour from another.
    """

    name = ""

    def __init__(self, api_key: str, model: str, base_url: str, timeout: float) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def model(self) -> str:
        return self._model

    # -- subclass contract --------------------------------------------------

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _body(self, messages: Sequence[dict[str, str]]) -> dict[str, Any]:
        raise NotImplementedError

    def _extract(self, payload: dict[str, Any]) -> Completion:
        """Turn a decoded response into a `Completion`, or raise `KeyError` /
        `IndexError` for a shape this provider does not recognise — which the
        loop below treats as a retryable failure rather than a crash."""
        raise NotImplementedError

    # -- the one method the platform calls ----------------------------------

    def complete(self, messages: Sequence[dict[str, str]]) -> Completion:
        if not self.configured:
            logger.warning("No API key configured for the {name} provider", name=self.name)
            return Completion(
                success=False,
                error=f"No API key configured for the {self.name} provider",
                provider=self.name,
            )

        last_error = ""
        for attempt in range(1, ATTEMPTS + 1):
            try:
                with httpx.Client(timeout=self._timeout) as client:
                    response = client.post(
                        self._base_url, headers=self._headers(), json=self._body(messages)
                    )
                    response.raise_for_status()
                    return self._extract(response.json())
            except (
                httpx.HTTPError,
                KeyError,
                IndexError,
                TypeError,
                json.JSONDecodeError,
            ) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "{name} request failed on attempt {attempt}: {error}",
                    name=self.name,
                    attempt=attempt,
                    error=last_error,
                )
                if attempt < ATTEMPTS:
                    time.sleep(BACKOFF_SECONDS * attempt)

        return Completion(success=False, error=last_error, model=self._model, provider=self.name)


def split_system(messages: Sequence[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """Separate the system prompt from the conversation.

    `PromptBuilder` emits OpenAI's shape, where the system prompt is a message
    with `role: "system"`. Anthropic takes it as a **top-level parameter** and
    rejects that role in `messages[0]`, so the translation has to happen
    somewhere. Here rather than in the prompt builder: the builder describes the
    reasoning task, and which wire format carries it is the provider's business.
    """
    system = "\n\n".join(
        message["content"] for message in messages if message.get("role") == "system"
    )
    rest = [dict(message) for message in messages if message.get("role") != "system"]
    return system, rest
