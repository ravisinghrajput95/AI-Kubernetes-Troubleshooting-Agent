"""The default suite never reaches a real model, whatever `backend/.env` holds.

With an Anthropic key in `backend/.env` the suite held open connections to the
Anthropic API: `Settings` reads the dotenv file, and nothing stopped tests that
build an analyzer from using it. CI has no `.env`, so only a developer's laptop
could see it — and only by noticing the suite had become slow.
"""

from app.core.config import Settings


def test_a_key_in_a_dotenv_file_does_not_reach_the_suite(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "OPENAI_API_KEY=sk-from-a-laptop\nANTHROPIC_API_KEY=sk-ant-from-a-laptop\n"
        "LLM_PROVIDER=anthropic\n"
    )

    configured = Settings(_env_file=dotenv)

    assert configured.openai_api_key == ""
    assert configured.anthropic_api_key == ""


def test_the_dotenv_file_is_otherwise_read(tmp_path, monkeypatch):
    """Vacuity: without the conftest's override the file does supply a key, so
    the test above is observing the guard rather than a file nobody reads."""
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    dotenv = tmp_path / ".env"
    dotenv.write_text("ANTHROPIC_API_KEY=sk-ant-from-a-laptop\n")

    assert Settings(_env_file=dotenv).anthropic_api_key == "sk-ant-from-a-laptop"
