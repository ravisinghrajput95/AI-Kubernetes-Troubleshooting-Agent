import json
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.auth.models import Principal
from app.core.config import settings
from app.kubernetes.command_policy import assert_read_only


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
    def __init__(
        self,
        context: str | None = None,
        principal: "Principal | None" = None,
    ) -> None:
        self.context = context
        self.principal = principal
        self.executed_commands: list[str] = []
        self._audit_lock = threading.Lock()

    def _impersonation_args(self, args: list[str]) -> list[str]:
        """Impersonation flags for the calling user.

        Without this, an authenticated user reads everything the service
        account's kubeconfig can reach — authentication alone would only decide
        *whether* you get in, not *what you can see*. With it, the cluster
        applies the user's own RBAC and the platform cannot exceed it.

        Local kubeconfig operations are excluded: there is no API server call to
        impersonate against.
        """
        if not settings.impersonate_users or self.principal is None:
            return []
        if self.principal.anonymous or args[:1] == ["config"]:
            return []

        flags = ["--as", self.principal.subject]
        for group in self.principal.groups:
            flags.extend(["--as-group", group])
        return flags

    def run(self, args: list[str], parse_json: bool = False) -> KubectlResult:
        assert_read_only(args)

        command = ["kubectl"]
        if self.context and args[:2] != ["config", "get-contexts"]:
            command.extend(["--context", self.context])
        command.extend(self._impersonation_args(args))
        command.extend(args)

        # Collectors run concurrently in worker threads; keep the audit trail intact.
        with self._audit_lock:
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
