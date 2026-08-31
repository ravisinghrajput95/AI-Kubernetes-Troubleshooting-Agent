"""What each provider actually sends, and what it does with the answer.

F11's remaining half: `LLMClient` posted to `api.openai.com` with the body
inlined, so a deployment that could not send its cluster's interior to OpenAI
had no option at all.

**Every assertion here is made on the request that reached the transport**, not
on the provider object. A header or a field that is built correctly and never
sent reads identically to a working one from inside the process — this
repository shipped exactly that defect with Loki's `X-Scope-OrgID`, which was
correct on an object that was never handed to the client. `MockTransport` is
what makes the difference visible.

The two wire formats differ in four ways that are each a 400 or a silent
degradation, and those differences are the entire argument for two classes
rather than one with a vendor flag: only a second implementation makes them
observable.
"""

import json

import httpx
import pytest

from app.ai.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    build_provider,
    split_system,
)
from app.ai.providers.base import ATTEMPTS

MESSAGES = [
    {"role": "system", "content": "You are a Senior Kubernetes SRE."},
    {"role": "user", "content": "SIGNALS: pod.crash_loop"},
]

OPENAI_REPLY = {
    "model": "gpt-4o-mini",
    "choices": [{"message": {"content": '{"root_cause": "config"}'}}],
}

ANTHROPIC_REPLY = {
    "model": "claude-opus-5",
    "stop_reason": "end_turn",
    "content": [{"type": "text", "text": '{"root_cause": "config"}'}],
}


class Wire:
    """Captures the request each provider makes, and answers with a canned body."""

    def __init__(self, reply: dict, status: int = 200) -> None:
        self.reply = reply
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.reply)

    @property
    def body(self) -> dict:
        return json.loads(self.requests[-1].content)

    @property
    def headers(self) -> httpx.Headers:
        return self.requests[-1].headers


@pytest.fixture
def wire(monkeypatch):
    """Install a transport under whichever provider the test builds."""

    def install(reply, status=200):
        captured = Wire(reply, status)
        original = httpx.Client

        def patched(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(captured)
            return original(*args, **kwargs)

        monkeypatch.setattr("app.ai.providers.base.httpx.Client", patched)
        return captured

    return install


class TestOpenAI:
    def test_the_request_is_what_it_always_was(self, wire):
        captured = wire(OPENAI_REPLY)
        provider = OpenAIProvider(
            api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/x", timeout=5
        )

        result = provider.complete(MESSAGES)

        assert result.success
        assert result.content == '{"root_cause": "config"}'
        assert captured.headers["authorization"] == "Bearer sk-test"
        assert captured.body["model"] == "gpt-4o-mini"
        assert captured.body["temperature"] == 0.1
        assert captured.body["response_format"] == {"type": "json_object"}
        # The system prompt stays a message here. That is the difference.
        assert captured.body["messages"][0]["role"] == "system"


class TestAnthropic:
    """Four differences, each of which fails the request if got wrong."""

    def test_the_system_prompt_becomes_a_top_level_parameter(self, wire):
        captured = wire(ANTHROPIC_REPLY)
        provider = AnthropicProvider(
            api_key="sk-ant", model="claude-opus-5", base_url="https://x/v1/messages", timeout=5
        )

        assert provider.complete(MESSAGES).success
        assert captured.body["system"] == "You are a Senior Kubernetes SRE."
        roles = [message["role"] for message in captured.body["messages"]]
        assert "system" not in roles, (
            "a system-role message in `messages` is rejected by the Messages API"
        )
        assert roles == ["user"]

    def test_temperature_is_never_sent(self, wire):
        """Removed on current models — sending it returns a 400. The OpenAI
        provider sends 0.1, so copying its body would fail every request."""
        captured = wire(ANTHROPIC_REPLY)
        AnthropicProvider(
            api_key="sk-ant", model="claude-opus-5", base_url="https://x", timeout=5
        ).complete(MESSAGES)

        assert "temperature" not in captured.body

    def test_max_tokens_is_always_sent(self, wire):
        """Required here, defaulted by OpenAI. Its absence is an error; too low
        a value truncates the diagnosis JSON mid-object."""
        captured = wire(ANTHROPIC_REPLY)
        AnthropicProvider(
            api_key="sk-ant",
            model="claude-opus-5",
            base_url="https://x",
            timeout=5,
            max_tokens=4096,
        ).complete(MESSAGES)

        assert captured.body["max_tokens"] == 4096

    def test_authentication_is_a_key_header_not_a_bearer_token(self, wire):
        captured = wire(ANTHROPIC_REPLY)
        AnthropicProvider(
            api_key="sk-ant", model="claude-opus-5", base_url="https://x", timeout=5
        ).complete(MESSAGES)

        assert captured.headers["x-api-key"] == "sk-ant"
        assert captured.headers["anthropic-version"]
        assert "authorization" not in captured.headers

    def test_only_text_blocks_become_content(self, wire):
        """A response carries more than the answer, and the filter is on the
        block *type* rather than on the presence of a `text` key.

        Thinking blocks alone would not prove this — they carry `thinking`, not
        `text`, so a provider that joined everything would still produce the
        right string and the check would pass while guarding nothing. The
        non-text block below carries a `text` field precisely so removing the
        type filter corrupts the JSON, which is what makes this defensive
        measure observable at all.
        """
        wire(
            {
                "model": "claude-opus-5",
                "stop_reason": "end_turn",
                "content": [
                    {"type": "thinking", "thinking": "considering the signals"},
                    {"type": "server_tool_use", "text": "SEARCHING"},
                    {"type": "text", "text": '{"root_cause": "config"}'},
                ],
            }
        )
        result = AnthropicProvider(
            api_key="sk-ant", model="claude-opus-5", base_url="https://x", timeout=5
        ).complete(MESSAGES)

        assert result.content == '{"root_cause": "config"}'
        import json as _json

        _json.loads(result.content)

    def test_a_refusal_is_reported_as_one(self, wire):
        """A refusal arrives as **HTTP 200** with no usable content. Reading
        `content` without checking `stop_reason` hands an empty string to the
        JSON parser and reports "unparseable" — losing the one fact that
        explains it."""
        wire(
            {
                "model": "claude-opus-5",
                "stop_reason": "refusal",
                "stop_details": {"type": "refusal", "category": "cyber"},
                "content": [],
            }
        )
        result = AnthropicProvider(
            api_key="sk-ant", model="claude-opus-5", base_url="https://x", timeout=5
        ).complete(MESSAGES)

        assert not result.success
        assert "declined" in result.error
        assert "cyber" in result.error

    def test_truncation_says_it_was_truncated(self, wire):
        """Content is real but cut off. "unparseable" would send someone
        looking at the prompt instead of at the ceiling."""
        wire(
            {
                "model": "claude-opus-5",
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": '{"root_cause": "conf'}],
            }
        )
        result = AnthropicProvider(
            api_key="sk-ant", model="claude-opus-5", base_url="https://x", timeout=5
        ).complete(MESSAGES)

        assert not result.success
        assert "LLM_MAX_TOKENS" in result.error


class TestOpenAICompatible:
    def test_it_posts_the_openai_shape_somewhere_else(self, wire):
        captured = wire(OPENAI_REPLY)
        provider = OpenAICompatibleProvider(
            api_key="", model="llama-3.3", base_url="http://vllm.internal:8000/v1/chat", timeout=5
        )

        assert provider.complete(MESSAGES).success
        assert str(captured.requests[-1].url) == "http://vllm.internal:8000/v1/chat"
        assert captured.body["model"] == "llama-3.3"

    def test_it_needs_no_api_key(self):
        """A model on a private network commonly has no auth. Requiring a key
        would mean inventing a placeholder, which is how `"none"` ends up
        committed in a values file."""
        provider = OpenAICompatibleProvider(
            api_key="", model="llama-3.3", base_url="http://vllm.internal:8000", timeout=5
        )
        assert provider.configured

    def test_it_sends_no_authorization_header_without_a_key(self, wire):
        captured = wire(OPENAI_REPLY)
        OpenAICompatibleProvider(
            api_key="", model="llama-3.3", base_url="http://vllm.internal:8000", timeout=5
        ).complete(MESSAGES)

        assert "authorization" not in captured.headers

    def test_it_reports_itself_separately_from_openai(self, wire):
        """A local model and OpenAI producing the same output is a claim worth
        being able to check; one shared provider name makes it uncheckable."""
        wire(OPENAI_REPLY)
        result = OpenAICompatibleProvider(
            api_key="", model="llama-3.3", base_url="http://vllm.internal:8000", timeout=5
        ).complete(MESSAGES)

        assert result.provider == "compatible"


class TestFailuresDegradeRatherThanRaise:
    def test_an_http_error_returns_a_failed_completion(self, wire):
        wire({"error": "nope"}, status=500)
        result = OpenAIProvider(api_key="sk", model="m", base_url="https://x", timeout=1).complete(
            MESSAGES
        )

        assert not result.success
        assert result.error

    def test_it_retries_before_giving_up(self, wire, monkeypatch):
        monkeypatch.setattr("app.ai.providers.base.time.sleep", lambda _: None)
        captured = wire({"error": "nope"}, status=500)

        OpenAIProvider(api_key="sk", model="m", base_url="https://x", timeout=1).complete(MESSAGES)

        assert len(captured.requests) == ATTEMPTS

    def test_an_unconfigured_provider_does_not_call_out(self, wire):
        captured = wire(OPENAI_REPLY)
        result = OpenAIProvider(api_key="", model="m", base_url="https://x", timeout=1).complete(
            MESSAGES
        )

        assert not result.success
        assert captured.requests == [], "a keyless provider still made a request"

    def test_an_unrecognised_response_shape_is_a_failure_not_a_crash(self, wire, monkeypatch):
        monkeypatch.setattr("app.ai.providers.base.time.sleep", lambda _: None)
        wire({"unexpected": "shape"})
        result = OpenAIProvider(api_key="sk", model="m", base_url="https://x", timeout=1).complete(
            MESSAGES
        )

        assert not result.success


class TestChoosingAProvider:
    def test_an_openai_key_alone_still_means_openai(self, monkeypatch):
        """Today's behaviour preserved exactly. Nobody who configured
        `OPENAI_API_KEY` and nothing else should find their investigations going
        somewhere different after an upgrade."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "llm_provider", "")
        monkeypatch.setattr(settings, "openai_api_key", "sk-openai")
        monkeypatch.setattr(settings, "anthropic_api_key", "")

        assert build_provider(settings).name == "openai"

    def test_an_anthropic_key_alone_is_inferred(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "llm_provider", "")
        monkeypatch.setattr(settings, "openai_api_key", "")
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")

        assert build_provider(settings).name == "anthropic"

    def test_both_keys_set_with_nothing_named_stays_on_openai(self, monkeypatch):
        """The backward-compatibility case that matters, and the one the other
        two miss by setting the unused key to empty.

        A deployment migrating from OpenAI to Anthropic has both keys set for a
        while. Inference must keep answering `openai` until the operator says
        otherwise — order in that list is the compatibility guarantee, not a
        preference.
        """
        from app.core.config import settings

        monkeypatch.setattr(settings, "llm_provider", "")
        monkeypatch.setattr(settings, "openai_api_key", "sk-openai")
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")

        assert build_provider(settings).name == "openai"

    def test_both_keys_set_does_not_guess(self, monkeypatch):
        """A deployment migrating from one to the other has both set for a
        while. Naming the provider has to win over inference, or the migration
        silently does not happen."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "llm_provider", "anthropic")
        monkeypatch.setattr(settings, "openai_api_key", "sk-openai")
        monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant")

        assert build_provider(settings).name == "anthropic"

    def test_nothing_configured_is_a_supported_outcome(self, monkeypatch):
        """`OPENAI_API_KEY` is optional by design: the deterministic pipeline
        produces a complete diagnosis marked `ai_generated: false`."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "llm_provider", "")
        monkeypatch.setattr(settings, "openai_api_key", "")
        monkeypatch.setattr(settings, "anthropic_api_key", "")
        monkeypatch.setattr(settings, "llm_base_url", "")

        provider = build_provider(settings)
        assert not provider.configured

    def test_an_unknown_provider_is_refused_at_startup(self, monkeypatch):
        """Not at first use. A typo that boots, keeps `/health` green and takes
        the deterministic path on every investigation is the hardest shape of
        misconfiguration to notice — the same argument as `validate_auth`."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "llm_provider", "gemini")

        with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
            settings.validate_llm()


class TestSplittingTheSystemPrompt:
    def test_it_leaves_the_conversation_alone(self):
        system, rest = split_system(MESSAGES)
        assert system == "You are a Senior Kubernetes SRE."
        assert rest == [{"role": "user", "content": "SIGNALS: pod.crash_loop"}]

    def test_it_does_not_mutate_the_caller_s_messages(self):
        """`PromptBuilder`'s output is reused when a playbook round re-analyses,
        so a provider that edited it in place would change the next call."""
        original = [dict(message) for message in MESSAGES]
        split_system(MESSAGES)
        assert original == MESSAGES

    def test_several_system_messages_are_joined(self):
        system, rest = split_system(
            [
                {"role": "system", "content": "first"},
                {"role": "system", "content": "second"},
                {"role": "user", "content": "go"},
            ]
        )
        assert system == "first\n\nsecond"
        assert len(rest) == 1

    def test_no_system_message_is_not_an_error(self):
        system, rest = split_system([{"role": "user", "content": "go"}])
        assert system == ""
        assert len(rest) == 1
