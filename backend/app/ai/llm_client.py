"""The reasoning layer's one call to a model.

Reduced to provider selection since F11. Everything about *how* a request is
shaped, retried and read now lives in `app/ai/providers/`, so choosing a
different model is configuration rather than an edit to the class that talks to
the network.

Kept as a class rather than collapsed into `build_provider` because the provider
is resolved **once per analyzer**, not once per process: a test that
monkeypatches `settings` gets the provider it configured, and a deployment that
reloads settings does not have to reach into a module global.
"""

from collections.abc import Sequence

from app.ai.providers import Completion, LLMProvider, build_provider


class LLMClient:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self._provider = provider

    @property
    def provider(self) -> LLMProvider:
        if self._provider is None:
            self._provider = build_provider()
        return self._provider

    def complete(self, messages: Sequence[dict[str, str]]) -> Completion:
        return self.provider.complete(messages)
