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

CHART = Path(__file__).resolve().parents[2] / "deploy" / "helm" / "k8s-agent"

if shutil.which("helm") is None:
    if os.environ.get("CI"):
        raise RuntimeError(
            "helm is not on PATH in CI, so the chart contract would silently not run"
        )
    pytest.skip("helm is not installed", allow_module_level=True)


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
            *[item for value in overrides for item in ("--set", value)],
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def configmap(*overrides: str) -> dict[str, str]:
    return next(item for item in render(*overrides) if item["kind"] == "ConfigMap")["data"]


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
