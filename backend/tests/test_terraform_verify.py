"""The Terraform check's contract with the chart: every key it sets must exist.

Helm ignores a values key the chart does not define — no warning, no error —
so Terraform producing `agentGateway.caSecret.certkey` would render, install and
leave the gateway on a development CA. `scripts/terraform_verify.py` refuses
such a key; this holds that refusal to the real chart, hermetically, because
the script itself needs terraform and helm and runs only in its own CI job.
"""

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "terraform_verify", ROOT / "scripts" / "terraform_verify.py"
)
terraform_verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(terraform_verify)

CHART = yaml.safe_load((ROOT / "deploy" / "helm" / "k8s-agent" / "values.yaml").read_text())


def test_keys_the_chart_defines_pass():
    values = {
        "auth": {"mode": "token", "tokensSecret": {"name": "t", "key": "API_TOKENS"}},
        "agentGateway": {"caSecret": {"name": "ca", "certKey": "ca.crt", "keyKey": "ca.key"}},
        "config": {"corsOrigins": ["https://agent.example.com"]},
    }
    assert terraform_verify.undefined_keys(values, CHART) == []


def test_a_misspelt_nested_key_is_named():
    values = {"agentGateway": {"caSecret": {"name": "ca", "certkey": "ca.crt"}}}
    assert terraform_verify.undefined_keys(values, CHART) == ["agentGateway.caSecret.certkey"]


def test_an_open_map_accepts_any_key():
    # `annotations: {}` is a map the operator fills, not a schema.
    values = {"ingress": {"annotations": {"alb.ingress.kubernetes.io/scheme": "internet-facing"}}}
    assert terraform_verify.undefined_keys(values, CHART) == []


def test_the_scenarios_reach_the_nested_keys():
    # Vacuity: the scenarios the script renders must themselves reach deep keys,
    # or the check above has nothing to judge in CI.
    scenarios = terraform_verify.SCENARIOS
    assert any(s["inputs"].get("agent_gateway", {}).get("enabled") for s in scenarios.values())
    assert any(s["inputs"].get("hostname") for s in scenarios.values())
