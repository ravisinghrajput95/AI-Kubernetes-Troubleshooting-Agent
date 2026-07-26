import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.core.config import settings


@dataclass
class KubectlResult:
    command: list[str]
    success: bool
    stdout: str
    stderr: str
    return_code: int
    data: dict[str, Any] | list[Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": " ".join(self.command),
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "return_code": self.return_code,
            "data": self.data,
        }


class KubectlExecutor:
    def __init__(self, context: str | None = None) -> None:
        self.context = context
        self.executed_commands: list[str] = []

    def run(self, args: list[str], parse_json: bool = False) -> KubectlResult:
        command = ["kubectl"]
        if self.context and args[:2] != ["config", "get-contexts"]:
            command.extend(["--context", self.context])
        command.extend(args)
        self.executed_commands.append(" ".join(command))
        env = os.environ.copy()

        if settings.kubeconfig_path:
            env["KUBECONFIG"] = settings.kubeconfig_path

        logger.info("Running command: {command}", command=" ".join(command))

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.kubectl_timeout_seconds,
                check=False,
                env=env,
            )
        except FileNotFoundError:
            logger.error("kubectl was not found on PATH")
            return KubectlResult(
                command=command,
                success=False,
                stdout="",
                stderr="kubectl was not found on PATH",
                return_code=127,
            )
        except subprocess.TimeoutExpired:
            logger.error("kubectl command timed out: {command}", command=" ".join(command))
            return KubectlResult(
                command=command,
                success=False,
                stdout="",
                stderr="kubectl command timed out",
                return_code=124,
            )

        data: dict[str, Any] | list[Any] | None = None
        success = completed.returncode == 0

        if success and parse_json:
            try:
                data = json.loads(completed.stdout or "{}")
            except json.JSONDecodeError as exc:
                success = False
                logger.error("Failed to parse kubectl JSON output: {error}", error=str(exc))

        if not success:
            logger.warning(
                "kubectl command failed: {command} stderr={stderr}",
                command=" ".join(command),
                stderr=completed.stderr.strip(),
            )

        return KubectlResult(
            command=command,
            success=success,
            stdout=completed.stdout,
            stderr=completed.stderr,
            return_code=completed.returncode,
            data=data,
        )
