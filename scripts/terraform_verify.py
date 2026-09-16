#!/usr/bin/env python3
"""Check deploy/terraform as far as it can be checked without a cloud account.

What this runs, in order:

1. `terraform fmt -check`, then `init`, `validate` and `test` for each module.
   The AWS module's tests mock every provider but `random`: they prove wiring,
   not that AWS accepts the resources.
2. For each scenario below, `terraform apply` of the provider-free
   `platform-values` module — which touches nothing — and then:
   - **every key it sets must exist in the chart's values.yaml.** Helm ignores
     a key the chart does not define, silently, so a misspelt
     `agentGateway.caSecret.certkey` renders, installs and does nothing;
   - **`helm template` of the real chart must accept the values**, so the
     chart's `_validate.tpl` judges what Terraform produced;
   - **the rendered ConfigMap must say what the scenario asked for**, read back
     through the platform's own `Settings` where that is how the value is
     consumed.

What it cannot run is an apply of `deploy/terraform/aws`. Nothing here has
created an RDS instance, an ElastiCache group or a Route 53 record, and a clean
run of this script is not evidence that one would work.

    python scripts/terraform_verify.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM = ROOT / "deploy" / "terraform"
CHART = ROOT / "deploy" / "helm" / "k8s-agent"
MODULES = [TERRAFORM / "modules" / "platform-values", TERRAFORM / "aws"]

SCENARIOS: dict[str, dict] = {
    "token-single-tenant": {
        "inputs": {
            "auth": {"mode": "token", "tokens_secret_name": "k8s-agent-tokens"},
            "state_secret_name": "k8s-agent-state",
            "hostname": "agent.example.com",
            "ingress_class_name": "alb",
            "ingress_tls_secret_name": "agent-example-com-tls",
        },
        "configmap": {
            "AUTH_MODE": "token",
            "TENANCY_MODE": "single",
            "CORS_ORIGINS": ["https://agent.example.com"],
        },
    },
    "oidc-shared-with-gateway": {
        "inputs": {
            "auth": {
                "mode": "oidc",
                "oidc_issuer": "https://idp.example.com",
                "oidc_audience": "k8s-agent",
                "oidc_tenant_claim": "org",
            },
            "tenancy_mode": "shared",
            "state_secret_name": "k8s-agent-state",
            "agent_gateway": {
                "enabled": True,
                "hostname": "gateway.agent.example.com",
                "ca_secret_name": "k8s-agent-ca",
            },
        },
        "configmap": {
            "AUTH_MODE": "oidc",
            "TENANCY_MODE": "shared",
            "AGENT_GATEWAY_ADVERTISE": "gateway.agent.example.com:9443",
        },
        "dns_name": "gateway.agent.example.com",
    },
}


def run(command: list[str], cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    where = cwd.relative_to(ROOT) if cwd.is_relative_to(ROOT) else cwd.name
    print(f"  $ {' '.join(command)}  ({where})", flush=True)
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, **kwargs)


def check(result: subprocess.CompletedProcess, what: str) -> None:
    if result.returncode != 0:
        raise SystemExit(f"FAIL {what}\n{result.stdout}\n{result.stderr}")


def undefined_keys(values: object, chart: object, path: str = "") -> list[str]:
    """Paths set in `values` that the chart's values.yaml does not define."""
    if not isinstance(values, dict):
        return []
    if not isinstance(chart, dict):
        return [path or "<root>"]
    missing = []
    for key, value in values.items():
        here = f"{path}.{key}" if path else key
        if key not in chart:
            missing.append(here)
        elif isinstance(value, dict) and chart[key] != {}:
            # `{}` in values.yaml is an open map (annotations, labels).
            missing.extend(undefined_keys(value, chart[key], here))
    return missing


def modules() -> None:
    print("\n== terraform fmt, init, validate, test")
    check(run(["terraform", "fmt", "-check", "-recursive"], TERRAFORM), "terraform fmt")
    for module in MODULES:
        check(
            run(["terraform", "init", "-backend=false", "-input=false"], module), f"init {module}"
        )
        check(run(["terraform", "validate", "-no-color"], module), f"validate {module}")
        result = run(["terraform", "test", "-no-color"], module)
        check(result, f"test {module}")
        print("    " + result.stdout.strip().splitlines()[-1])


def render(name: str, scenario: dict, chart_values: dict) -> None:
    print(f"\n== {name}")
    with tempfile.TemporaryDirectory(prefix="tf-values-") as scratch:
        work = Path(scratch)
        (work / "main.tf").write_text(
            f'module "values" {{\n  source = "{TERRAFORM / "modules" / "platform-values"}"\n'
            + "".join(f"  {key} = var.{key}\n" for key in scenario["inputs"])
            + "}\n"
            + "".join(f'variable "{key}" {{}}\n' for key in scenario["inputs"])
            + 'output "yaml" {\n  value = module.values.values_yaml\n}\n'
        )
        (work / "terraform.tfvars.json").write_text(json.dumps(scenario["inputs"]))
        check(run(["terraform", "init", "-input=false"], work), "init scenario")
        check(
            run(["terraform", "apply", "-auto-approve", "-input=false", "-no-color"], work), "apply"
        )
        output = run(["terraform", "output", "-raw", "yaml"], work)
        check(output, "output")
        values_file = work / "values.yaml"
        values_file.write_text(output.stdout)

        missing = undefined_keys(yaml.safe_load(output.stdout), chart_values)
        if missing:
            raise SystemExit(
                f"FAIL {name}: keys the chart does not define, which helm would ignore: {missing}"
            )
        print("    every key is one the chart defines")

        rendered = run(["helm", "template", "k8s-agent", str(CHART), "-f", str(values_file)], work)
        check(rendered, f"helm template {name}")
        print("    helm template accepted the values")

    documents = [item for item in yaml.safe_load_all(rendered.stdout) if item]
    data = next(item for item in documents if item["kind"] == "ConfigMap")["data"]
    for key, expected in scenario["configmap"].items():
        actual = json.loads(data[key]) if isinstance(expected, list) else data.get(key)
        if actual != expected:
            raise SystemExit(
                f"FAIL {name}: ConfigMap {key} is {data.get(key)!r}, expected {expected!r}"
            )
    if "CORS_ORIGINS" in scenario["configmap"]:
        settings_reads(data["CORS_ORIGINS"], scenario["configmap"]["CORS_ORIGINS"])
    if scenario.get("dns_name"):
        names = data["AGENT_GATEWAY_DNS_NAMES"].split(",")
        if scenario["dns_name"] not in names:
            raise SystemExit(
                f"FAIL {name}: the gateway certificate would not name {scenario['dns_name']}: {names}"
            )
    print("    the rendered ConfigMap says what was asked")


def settings_reads(rendered: str, expected: list[str]) -> None:
    """The platform, not this script, decides whether the rendered value parses."""
    backend = ROOT / "backend"
    python = backend / ".venv" / "bin" / "python"
    interpreter = str(python) if python.exists() else sys.executable
    result = subprocess.run(
        [
            interpreter,
            "-c",
            "from app.core.config import Settings; import json; print(json.dumps(Settings().cors_origins))",
        ],
        cwd=backend,
        env={**os.environ, "CORS_ORIGINS": rendered},
        capture_output=True,
        text=True,
    )
    check(result, "Settings() reading CORS_ORIGINS")
    if json.loads(result.stdout) != expected:
        raise SystemExit(
            f"FAIL the platform read CORS_ORIGINS as {result.stdout.strip()}, expected {expected}"
        )


def main() -> int:
    for tool in ("terraform", "helm"):
        if shutil.which(tool) is None:
            print(f"{tool} is not installed; nothing was checked", file=sys.stderr)
            return 2
    modules()
    chart_values = yaml.safe_load((CHART / "values.yaml").read_text())
    for name, scenario in SCENARIOS.items():
        render(name, scenario, chart_values)
    print(
        "\nOK — validated and tested with mocked providers, and the chart accepts the values."
        "\nNOT run: an apply against AWS."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
