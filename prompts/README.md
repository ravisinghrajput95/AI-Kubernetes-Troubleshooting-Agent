# Prompts

This folder holds human-readable prompt templates and guidance used by the backend AI modules.

Purpose
- Store prompt text, templates, and examples that are loaded by the various LLM-related components under `backend/app/ai/`.

How these are used
- The backend's `prompt_builder.py` and `llm_client.py` assemble and send prompts to an LLM. Prompt templates here should be written to be combined with runtime context (Kubernetes events, logs, resource manifests, investigation history).

Conventions
- File names: use descriptive names (e.g., `root_cause.md`, `fix_recommendation.md`).
- Format: plain text or Markdown. Keep placeholders obvious (e.g., `{{pod_name}}`, `{{logs}}`).
- Small examples: include a short example input + expected model instruction when useful.

Adding or updating prompts
- Make iterative edits; prompts are not code — update them based on model feedback and investigation results.
- When changing a prompt that is already referenced by code, also add a short note in the prompt file explaining the expected placeholders and the module that consumes it.

Location of related code
- See the AI modules in `backend/app/ai/` such as `prompt_builder.py`, `llm_client.py`, `root_cause_analyzer.py`, and `fix_recommendation_engine.py` for how prompts are loaded and used.

Notes
- These files are documentation-first artifacts: editing prompts does not change application logic, but can materially change model outputs. Test prompts in an isolated environment before applying them broadly.

