"""Name-neutral composition of independent trusted tool providers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aiweekend_target.tools.core import (
    ToolProtocolError,
    ToolProvider,
    ToolSpec,
    UnknownToolError,
)


class CompositeToolProvider:
    """Route by metadata ownership, never by domain-specific name branches."""

    def __init__(
        self,
        providers: Sequence[ToolProvider],
        *,
        allowlist: Sequence[str] | None = None,
    ) -> None:
        self._providers = tuple(providers)
        self._allowlist = frozenset(allowlist) if allowlist is not None else None
        self._specs: tuple[ToolSpec, ...] | None = None
        self._owners: Mapping[str, ToolProvider] | None = None

    async def _load(self) -> None:
        if self._specs is not None:
            return
        specs: list[ToolSpec] = []
        owners: dict[str, ToolProvider] = {}
        for provider in self._providers:
            for spec in await provider.list_tools():
                if not isinstance(spec, ToolSpec):
                    raise ToolProtocolError("tool provider returned invalid metadata")
                if self._allowlist is not None and spec.name not in self._allowlist:
                    continue
                if spec.name in owners:
                    raise ToolProtocolError(f"duplicate tool: {spec.name}")
                specs.append(spec)
                owners[spec.name] = provider
        if self._allowlist is not None and set(owners) != set(self._allowlist):
            missing = sorted(set(self._allowlist) - set(owners))
            raise UnknownToolError(f"allowlisted tools are unavailable: {', '.join(missing)}")
        self._specs = tuple(specs)
        self._owners = owners

    async def list_tools(self) -> tuple[ToolSpec, ...]:
        await self._load()
        assert self._specs is not None
        return self._specs

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        await self._load()
        assert self._owners is not None
        provider = self._owners.get(name)
        if provider is None:
            raise UnknownToolError(f"tool is not allowlisted: {name}")
        return await provider.call_tool(name, arguments)


__all__ = ["CompositeToolProvider"]
