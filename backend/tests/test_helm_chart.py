"""The chart's rendered environment must be one the platform can start with.

`config.corsOrigins` rendered `CORS_ORIGINS` comma-joined; `cors_origins` is a
list setting, which pydantic-settings reads from the environment as JSON, so
any value at all failed startup with a SettingsError. The integration job
installs the chart and never sets it. Found while writing the Terraform that
does — so the assertion here is the one two products have to agree on: what
`helm template` writes, read back by the platform's own `Settings`.

Skips only where `helm` is absent *and* this is not CI: GitHub's runners ship
helm, and a skip there would be a check that never runs.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "k8s-agent"

if shutil.which("helm") is None:
    if os.environ.get("CI"):
        raise RuntimeError(
            "helm is not on PATH in CI, so the chart contract would silently not run"
        )
    pytest.skip("helm is not installed", allow_module_level=True)


IMAGE = "registry.example.test/k8s-agent-backend"


def render(*overrides: str) -> list[dict]:
    result = subprocess.run(
        [
            "helm",
            "template",
            "t",
            str(CHART),
            "--set",
            "auth.mode=token",
            "--set",
            "auth.tokensSecret.name=tokens",
            "--set",
            "replicaCount=1",
            # Required, like every other caller: no backend image is published.
            "--set",
            f"image.repository={IMAGE}",
            *[item for value in overrides for item in ("--set", value)],
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def configmap(*overrides: str) -> dict[str, str]:
    return next(item for item in render(*overrides) if item["kind"] == "ConfigMap")["data"]


def test_the_chart_refuses_an_empty_image():
    """The default is published now, so this refuses an *emptied* value.

    It is kept because the shape it refuses shipped: the default named an
    unpublished `ghcr.io` path for several milestones, so the chart rendered
    cleanly and the pods sat in ImagePullBackOff. Nothing here noticed, because
    every path in this repository — the verify values, both Terraform roots,
    this file — sets its own image.
    """
    result = subprocess.run(
        [
            "helm",
            "template",
            "t",
            str(CHART),
            "--set",
            "auth.mode=token",
            "--set",
            "auth.tokensSecret.name=tokens",
            "--set",
            "replicaCount=1",
            "--set",
            "image.repository=",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, "the chart rendered with no image to pull"
    assert "image.repository" in result.stderr, (
        f"the refusal must name the value to set; got {result.stderr[:200]!r}"
    )


def test_the_default_image_is_the_one_ci_publishes():
    """The chart's default and the workflow that publishes it must name the
    same image, or the default is a path nobody pushes to — which is exactly
    what shipped, for several milestones, with no published image at all."""
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "backend-image.yml").read_text())
    published = workflow["env"]["IMAGE"].split("/")[-1]
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    default = values["image"]["repository"]

    assert default.endswith(f"/{published}"), (
        f"the chart defaults to {default!r}, which is not the {published!r} image "
        f"backend-image.yml publishes"
    )
    assert default.startswith(f"{workflow['env']['REGISTRY']}/"), (
        f"the chart defaults to {default!r}, not the registry the workflow pushes to"
    )


def test_the_image_given_is_the_image_deployed():
    """With a repository the chart renders, and the tag follows appVersion — so
    a chart version pulls the image that version published."""
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    deployment = next(item for item in render() if item["kind"] == "Deployment")
    containers = deployment["spec"]["template"]["spec"]["containers"]

    assert [c["image"] for c in containers] == [f"{IMAGE}:{chart['appVersion']}"]


@pytest.mark.parametrize(
    "origins",
    [
        ["https://agent.example.com"],
        ["https://agent.example.com", "https://console.example.com:8443"],
    ],
)
def test_cors_origins_render_as_the_platform_reads_them(origins, monkeypatch):
    rendered = configmap(f"config.corsOrigins={{{','.join(origins)}}}")["CORS_ORIGINS"]
    monkeypatch.setenv("CORS_ORIGINS", rendered)
    assert Settings().cors_origins == origins


def test_unset_cors_origins_leave_the_platform_default():
    assert "CORS_ORIGINS" not in configmap()


def test_extra_volumes_reach_the_platform_container():
    # A CA bundle for a Postgres or Redis on a private root has to be mountable,
    # or verify-full is unreachable from a chart deployment.
    documents = render(
        "extraVolumes[0].name=state-ca",
        "extraVolumes[0].secret.secretName=k8s-agent-state-ca",
        "extraVolumeMounts[0].name=state-ca",
        "extraVolumeMounts[0].mountPath=/etc/k8s-agent/trust",
    )
    spec = next(item for item in documents if item["kind"] == "Deployment")["spec"]["template"][
        "spec"
    ]
    container = spec["containers"][0]
    assert {"name": "state-ca", "secret": {"secretName": "k8s-agent-state-ca"}} in spec["volumes"]
    assert {"name": "state-ca", "mountPath": "/etc/k8s-agent/trust"} in container["volumeMounts"]
