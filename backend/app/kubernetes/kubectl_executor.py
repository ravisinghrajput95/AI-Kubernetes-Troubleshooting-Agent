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
from app.kubernetes.list_limit import cap_items


@dataclass
class KubectlResult:
    command: list[str]
    success: bool
    stdout: str
    stderr: str
    return_code: int
    data: dict[str, Any] | list[Any] | None = None
    # Set when a list response exceeded `max_list_items` and was capped. The
    # investigation is then working from a partial view of the cluster, which
    # must be visible rather than silently applied.
    truncated: bool = False
    total_items: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": " ".join(self.command),
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "return_code": self.return_code,
            "data": self.data,
            "truncated": self.truncated,
            "total_items": self.total_items,
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
        self.truncations: list[dict[str, Any]] = []
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
        from app.auth.impersonation import identity_for

        # Local kubeconfig operations have no API server call behind them, so
        # there is nothing to impersonate against. Everything else defers to the
        # one shared decision, so this path and the agent path cannot disagree
        # about who a read runs as.
        if args[:1] == ["config"]:
            return []

        identity = identity_for(self.principal)
        if identity is None:
            return []

        subject, groups = identity
        flags = ["--as", subject]
        for group in groups:
            flags.extend(["--as-group", group])
        return flags

    def _is_list_read(self, args: list[str]) -> bool:
        """True for `get <resource>` without a named object.

        A named read returns one object; only list reads can be unbounded.
        """
        if not args or args[0] != "get" or len(args) < 2:
            return False
        return len(args) < 3 or args[2].startswith("-")

    def _chunk_args(self, args: list[str]) -> list[str]:
        """Ask the API server to page large lists.

        This bounds apiserver and etcd memory per request. It does not bound
        this process: kubectl still assembles the whole list before writing it
        out, so peak parse memory remains proportional to cluster size. Removing
        that ceiling requires a streaming client — see docs/PRODUCTION_READINESS.md
        (F5).
        """
        if not self._is_list_read(args) or settings.kubectl_chunk_size <= 0:
            return []
        if any(arg.startswith("--chunk-size") for arg in args):
            return []
        return [f"--chunk-size={settings.kubectl_chunk_size}"]

    def _cap_items(self, data: Any, command: list[str]) -> tuple[Any, bool, int]:
        """Cap a list response, recording that it happened.

        The rule itself is `app.providers.list_limit.cap_items`, shared with the
        agent provider — which applied no cap at all until that was extracted,
        so the same cluster read two ways disagreed about how many pods it had.
        What stays here is this executor's own bookkeeping: the audit lock,
        because collectors run in worker threads, and the warning line.
        """
        rendered = " ".join(command)
        data, truncation, total = cap_items(data, rendered, settings.max_list_items)
        if truncation is None:
            return data, False, total

        logger.warning(
            "Capping list response at {limit} of {total} items: {command}",
            limit=truncation["retained"],
            total=total,
            command=rendered,
        )
        with self._audit_lock:
            self.truncations.append(truncation)
        return data, True, total

    def run(self, args: list[str], parse_json: bool = False) -> KubectlResult:
        assert_read_only(args)

        command = ["kubectl"]
        if self.context and args[:2] != ["config", "get-contexts"]:
            command.extend(["--context", self.context])
        command.extend(self._impersonation_args(args))
        command.extend(self._chunk_args(args))
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
        truncated = False
        total_items = 0

        if success and parse_json:
            try:
                data = json.loads(completed.stdout or "{}")
                data, truncated, total_items = self._cap_items(data, command)
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
            truncated=truncated,
            total_items=total_items,
        )
