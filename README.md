# AI Kubernetes Agent

Overview
This repository implements an observability and investigation assistant that helps diagnose Kubernetes failures using automated inspection and LLM-assisted analysis. It contains a FastAPI backend that inspects cluster state, collectors and inspectors for Kubernetes resources and logs, AI reasoning modules that build prompts and query language models, and a Vite/React frontend that presents investigations and reports.

Key components
- `backend/`: FastAPI application and AI logic.
  - `app/ai/`: LLM integration, prompt building, evidence redaction, root-cause analysis, confidence scoring, and fix-recommendation engines.
  - `app/kubernetes/`: inspectors, executors, log collectors and event analyzers that gather pod, deployment, network, node, storage, and workload context.
  - `app/api/`: HTTP routes used by the frontend and external callers (e.g., `investigate.py`, `health.py`).
  - `app/services/`: persistence and investigation orchestration (e.g., `investigation_service.py`, `history_service.py`).
  - `app/models/`: pydantic models for investigations and health representations.

- `frontend/`: Vite + React UI serving investigation lists, reports, and details.
- `data/`: persisted investigation history and generated reports (`data/investigations/`).
- `prompts/`: human-editable LLM prompt templates and guidance.
- `docs/`: documentation, architecture and workflow descriptions.

How it works (high level)
1. Kubernetes events and resource state are collected by components under `backend/app/kubernetes/` (inspectors, log collectors, executors).
2. Evidence can be scoped by cluster context, namespace, pod, or deployment to reduce noise and focus the investigation.
3. Collected context is redacted and passed to AI modules under `backend/app/ai/` which assemble prompts (using `prompt_builder.py`) and call `llm_client.py` to request analysis.
4. AI outputs and deterministic fallbacks produce root cause analysis, recommended fixes, evidence gaps, next steps, remediation risk, and rollback guidance.
5. Results are stored via the services layer and saved as investigation reports under `data/investigations/`.
6. The frontend queries the backend APIs to display investigations, workflow progress, topology, reports, and history to users.

Roadmap

Architecture enhancements

1. Explainable AI RCA
   - Expand confidence scoring from a single percentage into weighted evidence contributions.
   - Example breakdown:
     - Events Analysis: 35%
     - Network Analysis: 25%
     - Logs Analysis: 20%
     - Deployment Analysis: 20%
   - Benefits:
     - Builds trust in AI recommendations.
     - Helps SRE teams validate whether a diagnosis is supported by real evidence.
     - Makes weak or missing signals visible during incident review.

2. Cluster Topology Visualization
   - Generate an interactive graph that maps Kubernetes relationships:

     ```text
     Ingress
        |
     Service
        |
     Deployment
        |
     Pods
        |
     Nodes
     ```

   - Planned features:
     - Click-to-investigate for any graph node.
     - Highlight unhealthy components.
     - Show traffic flow and service-to-pod endpoint health.
     - Overlay events, restart counts, readiness, and node pressure.

3. Multi-Agent Collaboration Timeline
   - Make the investigation workflow transparent by showing each agent contribution.
   - Example:
     - Pod Agent -> Completed
     - Logs Agent -> Found 15 error lines
     - Event Agent -> Found CrashLoopBackOff evidence
     - Network Agent -> Connectivity verified
     - RCA Agent -> Generated root cause and remediation plan
   - Benefits:
     - Makes AI-assisted troubleshooting auditable.
     - Helps users understand which evidence influenced the final RCA.
     - Improves handoff quality for incident reports and postmortems.

Running locally (development)
- Backend (from `backend/`):

  1. Create and activate a virtual environment and install dependencies from `requirements.txt`.
     - Example (PowerShell):

       ```powershell
       python -m venv .venv
       .\.venv\Scripts\Activate.ps1
       pip install -r requirements.txt
       ```

  2. Start the FastAPI server:

       ```powershell
       uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
       ```

- Frontend (from `frontend/`):

  1. Install dependencies and start the dev server:

       ```bash
       npm install
       npm run dev
       ```

- Or use `docker-compose.yml` to bring services up in containers when appropriate.

Important files to explore
- Backend entry: `backend/app/main.py` and API routes in `backend/app/api/`.
- AI logic: `backend/app/ai/prompt_builder.py`, `llm_client.py`, `root_cause_analyzer.py`, `fix_recommendation_engine.py`.
- Inspectors and collectors: `backend/app/kubernetes/*` (e.g., `logs_collector.py`, `pod_inspector.py`, `node_inspector.py`, `storage_inspector.py`).
- Persistence and investigations: `backend/app/services/investigation_service.py` and `data/investigations/`.

Notes and constraints
- Prompt files live under `prompts/` and should be iterated on with model testing; they can change behavior without code edits.
- Local investigation history is currently stored under `data/investigations/`; production deployments should use database-backed persistence, authentication, RBAC, audit trails, and secret-safe evidence handling.

Where to look next
- Review `docs/PROJECT_OVERVIEW.md` for an architecture diagram and detailed component responsibilities.
- Inspect `backend/app/ai/` if you want to trace exactly how prompts are constructed and sent to the LLM.
