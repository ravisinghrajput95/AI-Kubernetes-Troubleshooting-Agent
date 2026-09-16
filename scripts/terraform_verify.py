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

3. With `--kind`, **an apply**: `deploy/terraform/kind` installs the same
   `modules/platform-release` the AWS root uses into a kind cluster, beside a
   Postgres that refuses plaintext and a Redis that requires its password, and
   then requires that:
   - the release became ready — which, because readiness consults Postgres,
     means the platform connected with the URL the module formatted;
   - every platform connection Postgres sees is TLS, **and** a plaintext
     connection is refused, without which the first check proves nothing;
   - `/health/ready` reports both stores ok from inside a platform pod;
   - the token the module wrote authenticates, and a wrong one does not.
   It destroys what it applied unless `--keep`.

What it cannot run is an apply of `deploy/terraform/aws`. Nothing here has
created an RDS instance, an ElastiCache group or a Route 53 record, and a clean
run of this script is not evidence that one would work.

    python scripts/terraform_verify.py
    python scripts/terraform_verify.py --kind --kubeconfig ~/.kube/config \
        --context kind-dev --image k8s-agent-backend:tfverify   # loaded into kind
"""

from __future__ import annotations

import argparse
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
MODULES = [
    TERRAFORM / "modules" / "platform-values",
    TERRAFORM / "modules" / "platform-release",
    TERRAFORM / "aws",
    TERRAFORM / "kind",
]

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


def run(command: list[str], cwd: Path, show: bool = True) -> subprocess.CompletedProcess:
    if show:
        where = cwd.relative_to(ROOT) if cwd.is_relative_to(ROOT) else cwd.name
        print(f"  $ {' '.join(command)}  ({where})", flush=True)
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True)


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
        if not (module / "tests").is_dir():
            continue
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


def kind(kubeconfig: str, context: str, image: str, keep: bool) -> None:
    root = TERRAFORM / "kind"
    repository, _, tag = image.rpartition(":")
    variables = [
        f"-var=kubeconfig_path={kubeconfig}",
        f"-var=kube_context={context}",
        f"-var=image_repository={repository}",
        f"-var=image_tag={tag}",
    ]
    kubectl = ["kubectl", "--kubeconfig", kubeconfig, "--context", context, "-n", "k8s-agent-tf"]

    def query(sql: str) -> str:
        result = run(
            [
                *kubectl,
                "exec",
                "deploy/postgres",
                "--",
                "psql",
                "-U",
                "k8sagent",
                "-d",
                "k8sagent",
                "-Atc",
                sql,
            ],
            root,
        )
        check(result, "psql")
        return result.stdout.strip()

    def platform_get(path: str, token: str) -> str:
        code = (
            "import urllib.request as u, urllib.error as e\n"
            f"r = u.Request('http://127.0.0.1:8000{path}', headers={{'Authorization': 'Bearer ' + {token!r}}})\n"
            "try:\n    response = u.urlopen(r)\n    print(response.status, response.read().decode())\n"
            "except e.HTTPError as x:\n    print(x.code)\n"
        )
        # Not echoed: the command carries the API token.
        result = run(
            [*kubectl, "exec", "deploy/k8s-agent", "--", "python", "-c", code], root, show=False
        )
        check(result, f"GET {path}")
        return result.stdout.strip()

    print("\n== apply deploy/terraform/kind")
    check(run(["terraform", "init", "-input=false"], root), "init kind")
    try:
        check(
            run(
                ["terraform", "apply", "-auto-approve", "-input=false", "-no-color", *variables],
                root,
            ),
            "apply kind",
        )
        print(
            "    applied; the release became ready (helm waited on readiness, which consults Postgres)"
        )

        by_tls = query(
            "SELECT s.ssl, count(*) FROM pg_stat_ssl s JOIN pg_stat_activity a USING (pid) "
            "WHERE a.client_addr IS NOT NULL AND a.usename = 'k8sagent' GROUP BY 1"
        )
        rows = dict(line.split("|") for line in by_tls.splitlines())
        if rows.get("f") or not rows.get("t"):
            raise SystemExit(f"FAIL platform connections by TLS: {rows or 'none at all'}")
        print(f"    {rows['t']} platform connection(s) to Postgres, all TLS")

        plaintext = run(
            [
                *kubectl,
                "exec",
                "deploy/postgres",
                "--",
                "sh",
                "-c",
                'PGPASSWORD="$POSTGRES_PASSWORD" psql "host=127.0.0.1 user=k8sagent dbname=k8sagent sslmode=disable" -c "select 1"',
            ],
            root,
        )
        if plaintext.returncode == 0 or "no encryption" not in plaintext.stderr:
            raise SystemExit(
                f"FAIL the control: Postgres accepted plaintext, so TLS proves nothing\n{plaintext.stderr}"
            )
        print("    control: Postgres refuses a plaintext connection")

        token = run(["terraform", "output", "-raw", "api_token"], root)
        check(token, "api_token output")
        ready = platform_get("/health/ready", token.stdout)
        if '"postgres":"ok"' not in ready or '"redis":"ok"' not in ready:
            raise SystemExit(f"FAIL /health/ready: {ready}")
        print("    /health/ready: postgres ok, redis ok")
        if not platform_get("/me", token.stdout).startswith("200"):
            raise SystemExit("FAIL the token the module wrote does not authenticate")
        if platform_get("/me", "not-the-token") != "401":
            raise SystemExit(
                "FAIL a wrong token was not refused, so the check above proves nothing"
            )
        print("    the module's token authenticates; a wrong one is refused")
    finally:
        if keep:
            print("    --keep: left applied")
        else:
            check(
                run(
                    [
                        "terraform",
                        "destroy",
                        "-auto-approve",
                        "-input=false",
                        "-no-color",
                        *variables,
                    ],
                    root,
                ),
                "destroy kind",
            )
            print("    destroyed")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--kind", action="store_true", help="also apply deploy/terraform/kind")
    parser.add_argument("--kubeconfig", default=os.path.expanduser("~/.kube/config"))
    parser.add_argument("--context", default="")
    parser.add_argument("--image", default="k8s-agent-backend:tfverify")
    parser.add_argument("--keep", action="store_true", help="do not destroy after --kind")
    arguments = parser.parse_args()
    if arguments.kind and not arguments.context:
        parser.error("--kind needs --context, so it never applies to whatever cluster is current")

    for tool in ("terraform", "helm", *(["kubectl"] if arguments.kind else [])):
        if shutil.which(tool) is None:
            print(f"{tool} is not installed; nothing was checked", file=sys.stderr)
            return 2
    modules()
    chart_values = yaml.safe_load((CHART / "values.yaml").read_text())
    for name, scenario in SCENARIOS.items():
        render(name, scenario, chart_values)
    if arguments.kind:
        kind(arguments.kubeconfig, arguments.context, arguments.image, arguments.keep)
        print(
            "\nOK — the Kubernetes half applied on kind and served; the AWS half is validated and mocked only."
            "\nNOT run: an apply against AWS."
        )
    else:
        print(
            "\nOK — validated and tested with mocked providers, and the chart accepts the values."
            "\nNOT run: an apply against AWS, or --kind."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
