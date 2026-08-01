"""Provider backed by a local kubeconfig and the `kubectl` binary.

This is the implementation the platform has always had, now behind the provider
interface. It remains the right choice for development and single-cluster
installs; fleet deployments use the remote agent provider instead.

Translating `ResourceRequest` into argv happens here and nowhere else. That is
the point of the abstraction: collectors describe evidence, one adapter decides
how to obtain it.
"""

import asyncio
from collections.abc import Sequence
from typing import Any

from app.auth.models import Principal
from app.kubernetes.kubectl_executor import KubectlExecutor, KubectlResult
from app.providers.base import (
    ClusterProvider,
    OutputFormat,
    ProviderResult,
    ReadVerb,
    ResourceRequest,
)


class LocalKubectlProvider(ClusterProvider):
    def __init__(
        self,
        context: str | None = None,
        principal: Principal | None = None,
        executor: KubectlExecutor | None = None,
    ) -> None:
        self._executor = executor or KubectlExecutor(context=context, principal=principal)
        self._cluster_id = context or "current-context"

    @property
    def cluster_id(self) -> str:
        return self._cluster_id

    @property
    def executed_commands(self) -> list[str]:
        return self._executor.executed_commands

    @property
    def truncations(self) -> list[dict[str, Any]]:
        return self._executor.truncations

    async def fetch(self, request: ResourceRequest) -> ProviderResult:
        args = self.to_args(request)
        parse_json = request.output is OutputFormat.JSON
        result = await asyncio.to_thread(self._executor.run, args, parse_json)
        return self._to_result(result)

    async def fetch_many(self, requests: Sequence[ResourceRequest]) -> Sequence[ProviderResult]:
        return await asyncio.gather(*(self.fetch(request) for request in requests))

    # -- request translation ------------------------------------------------

    @staticmethod
    def to_args(request: ResourceRequest) -> list[str]:
        """`ResourceRequest` → kubectl argv. The only place this mapping exists."""
        options = request.options

        if request.verb is ReadVerb.CONFIG:
            return ["config", str(options.get("subcommand", "view")), *_output(request)]

        if request.verb is ReadVerb.LOGS:
            args = ["logs", request.name or ""]
            args += _namespace(request)
            if options.get("previous"):
                args.append("--previous")
            if options.get("tail"):
                args.append(f"--tail={options['tail']}")
            if options.get("all_containers", True):
                args.append("--all-containers=true")
            return args

        if request.verb is ReadVerb.TOP:
            args = ["top", request.resource]
            if request.all_namespaces:
                args.append("-A")
            if options.get("no_headers", True):
                args.append("--no-headers")
            return args

        if request.verb is ReadVerb.DESCRIBE:
            return [
                "describe",
                request.resource,
                *([request.name] if request.name else []),
                *_namespace(request),
            ]

        # GET
        args = ["get", request.resource]
        if request.name:
            args.append(request.name)
        args += _namespace(request)
        if request.all_namespaces and not request.namespace:
            args.append("-A")
        if request.label_selector:
            args += ["-l", request.label_selector]
        if request.field_selector:
            args.append(f"--field-selector={request.field_selector}")
        args += _output(request)
        return args

    def _to_result(self, result: KubectlResult) -> ProviderResult:
        return ProviderResult(
            success=result.success,
            data=result.data,
            text=result.stdout,
            error=result.stderr,
            equivalent_command=" ".join(result.command),
            truncated=result.truncated,
            total_items=result.total_items,
        )


def _namespace(request: ResourceRequest) -> list[str]:
    return ["-n", request.namespace] if request.namespace else []


def _output(request: ResourceRequest) -> list[str]:
    return ["-o", "json"] if request.output is OutputFormat.JSON else []
