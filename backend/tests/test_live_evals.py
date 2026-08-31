"""The live-model eval gate, and the ways it must refuse to pass.

`evals/live.py` is the only thing in this repository that measures whether the
*configured model* produces answers grounding accepts. `docs/EVALUATION.md`
names why that matters: an over-strict grounding check does not fail, it
silently routes every investigation to the deterministic fallback while 20/20
golden cases keep passing.

A gate for that is only worth having if it cannot go green without doing the
work, so most of this file is about the refusals rather than the happy path.
The model is a **local HTTP stub speaking the chat-completions shape**, not a
patched provider: the thing under test is a real provider, real httpx, real
JSON parsing and real grounding, reached through `LLM_BASE_URL` exactly as an
OpenAI-compatible deployment would be. Substituting the provider would test the
scoring arithmetic against itself.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evals import live
from evals.runner import load_investigation_cases

GROUNDABLE = {
    "selected_hypothesis": "",  # filled per request from the prompt's own ids
    "root_cause": "The container is restarting because its configuration is incomplete.",
    "explanation": "The evidence shows repeated restarts with a configuration fault.",
    "cited_signals": [],
    "confidence": 70,
}


class Stub(BaseHTTPRequestHandler):
    """A chat-completions endpoint whose answer is chosen by the test."""

    behaviour = "grounded"
    calls = 0

    def log_message(self, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).calls += 1

        if type(self).behaviour == "error":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'{"error": "upstream is unwell"}')
            return

        answer = self._answer(body)
        payload = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _answer(self, body: dict) -> dict:
        prompt = "\n".join(message.get("content", "") for message in body.get("messages", []))
        answer = dict(GROUNDABLE)

        if type(self).behaviour == "ungrounded":
            # A hypothesis id that appears nowhere: grounding rejects outright.
            answer["selected_hypothesis"] = "workload.invented_by_the_model"
            answer["cited_signals"] = ["signal.that.does.not.exist:pod/nowhere/none"]
            return answer

        # Cite what the prompt actually offered, which is what a working model
        # does. The ids are in the prompt because that is the whole design:
        # the model selects and explains, it does not diagnose from raw JSON.
        answer["selected_hypothesis"] = _first_match(prompt, r"^\s*-?\s*([a-z_]+\.[a-z_]+)\b")
        signal = _first_match(prompt, r"\b([a-z_]+\.[a-z_.]+:[a-zA-Z0-9/_.-]+)")
        answer["cited_signals"] = [signal] if signal else []
        return answer


def _first_match(text: str, pattern: str) -> str:
    import re

    found = re.search(pattern, text, re.MULTILINE)
    return found.group(1) if found else ""


@pytest.fixture
def stub_model(monkeypatch):
    """A running stub, with the settings pointed at it."""
    from app.core.config import settings

    server = HTTPServer(("127.0.0.1", 0), Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Stub.calls = 0
    Stub.behaviour = "grounded"

    monkeypatch.setattr(settings, "llm_provider", "compatible")
    monkeypatch.setattr(settings, "openai_api_key", "stub-key")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(
        settings, "llm_base_url", f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    )
    try:
        yield Stub
    finally:
        server.shutdown()
        server.server_close()


class TestItCannotPassWithoutCallingAnything:
    """The failure mode that has bitten this repository repeatedly.

    A suite that skips, or that calls nothing and finds no problems, is
    indistinguishable from one that ran and passed. Every branch here must be
    something other than exit 0.
    """

    def test_no_configured_model_is_a_refusal_not_a_pass(self, monkeypatch, capsys):
        from app.core.config import settings

        monkeypatch.setattr(settings, "llm_provider", "")
        monkeypatch.setattr(settings, "openai_api_key", "")
        monkeypatch.setattr(settings, "anthropic_api_key", "")
        monkeypatch.setattr(settings, "llm_base_url", "")

        assert live.main([]) == 2
        assert "REFUSED" in capsys.readouterr().err

    def test_a_model_that_never_answers_is_a_refusal_not_a_clean_sheet(self, stub_model, capsys):
        """Zero rejections because zero answers is the vacuous pass.

        Every call fails, so nothing is rejected by grounding and the naive
        reading is "no grounding failures". The floor on answered cases is what
        makes that a refusal.
        """
        stub_model.behaviour = "error"

        assert live.main(["--min-grounded", "0.0", "--limit", "2"]) == 2
        captured = capsys.readouterr()
        assert "reached the model" in captured.err
        assert stub_model.calls > 0, "the stub was never called; this proves nothing"

    def test_an_empty_corpus_is_a_refusal(self, stub_model, monkeypatch, capsys):
        monkeypatch.setattr(live, "run", lambda limit=0: live.LiveReport(provider="p", model="m"))
        assert live.main([]) == 2
        assert "corpus is empty" in capsys.readouterr().err


class TestItScoresWhatTheModelActuallySaid:
    def test_groundable_answers_pass_the_gate(self, stub_model):
        assert live.main(["--limit", "4"]) == 0
        assert stub_model.calls == 4

    def test_ungroundable_answers_fail_the_gate(self, stub_model, capsys):
        """The regression this exists for.

        A prompt change that degrades a real model's answers, or a grounding
        rule tightened past what any model produces, both land here — and
        nowhere else. `python -m evals` stays 20/20 through either.
        """
        stub_model.behaviour = "ungrounded"

        assert live.main(["--limit", "4"]) == 1
        captured = capsys.readouterr()
        assert "survived grounding" in captured.err
        # And it says which way the failure went, because "the prompt drifted"
        # and "grounding tightened" need different fixes.
        assert "deterministic fallback" in captured.err

    def test_a_rejected_answer_still_counts_as_answered(self, stub_model):
        """Otherwise a total grounding failure looks like a total outage.

        The two need opposite responses — one is a reasoning regression, the
        other an infrastructure problem — so the report must not collapse them.
        """
        stub_model.behaviour = "ungrounded"
        report = live.run(limit=3)

        assert len(report.answered) == 3, "a rejected answer was scored as no answer at all"
        assert report.grounded == []
        assert report.grounded_rate == 0.0
        assert all(case.rejection for case in report.cases)
        # And the reason is grounding's, not the deterministic fallback's own
        # "no model output was used" — which is what the first version reported
        # for a total outage, scoring twenty failures as twenty clean answers.
        assert not any("no model output was used" in case.rejection for case in report.cases)

    def test_the_corpus_is_the_one_the_offline_evals_use(self):
        """Not a second, friendlier set of cases.

        A live suite with its own corpus measures the corpus. Sharing the files
        means a regression in either path is scored against the same ground
        truth.
        """
        assert live.load_investigation_cases is load_investigation_cases
        assert len(load_investigation_cases()) >= 20
