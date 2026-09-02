"""Explicit host-owned registry for trusted, versioned components."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TypeAlias

from aiweekend_target.core.contracts import checked_identifier, detached_json


MAX_COMPONENT_CONFIG_BYTES = 64 * 1024
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,2}$")
_FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "command",
        "credential",
        "endpoint",
        "import",
        "module",
        "password",
        "secret",
        "token",
        "url",
    }
)


class ComponentKind(StrEnum):
    MODEL = "model"
    TOOL_PROVIDER = "tool_provider"
    ANALYZER = "analyzer"
    POLICY = "policy"
    ATTACK = "attack"
    EVALUATOR = "evaluator"


def _checked_config(value: object, *, depth: int = 0) -> object:
    if depth > 12:
        raise ValueError("component config is too deeply nested")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 32 * 1024:
            raise ValueError("component config contains oversized text")
        if "://" in value or value.startswith(("python:", "module:")):
            raise ValueError("component config contains a forbidden locator")
        return value
    if isinstance(value, list):
        if len(value) > 256:
            raise ValueError("component config contains an oversized array")
        return [_checked_config(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise ValueError("component config contains an oversized object")
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key.encode("utf-8")) > 128:
                raise ValueError("component config key is invalid")
            normalized = key.casefold().replace("-", "_")
            if normalized in _FORBIDDEN_CONFIG_KEYS:
                raise ValueError(f"component config field is forbidden: {key}")
            result[key] = _checked_config(item, depth=depth + 1)
        return result
    raise ValueError("component config must contain only JSON values")


@dataclass(frozen=True)
class ComponentRef:
    kind: ComponentKind
    id: str
    version: str
    config: Mapping[str, object] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ComponentKind):
            raise ValueError("component kind is invalid")
        checked_identifier(self.id, "component id")
        if not isinstance(self.version, str) or not _VERSION.fullmatch(self.version):
            raise ValueError("component version is invalid")
        checked = _checked_config(self.config)
        checked = detached_json(
            checked, maximum=MAX_COMPONENT_CONFIG_BYTES, label="component config"
        )
        if not isinstance(checked, dict):
            raise ValueError("component config must be an object")
        object.__setattr__(self, "config", MappingProxyType(checked))

    @property
    def key(self) -> tuple[ComponentKind, str, str]:
        return self.kind, self.id, self.version


ComponentFactory: TypeAlias = Callable[[Mapping[str, object]], object]
ConfigValidator: TypeAlias = Callable[[Mapping[str, object]], None]


@dataclass(frozen=True)
class _Registration:
    factory: ComponentFactory
    validate: ConfigValidator | None


class ComponentCatalog:
    """Resolve manifests only through factories installed by trusted host code."""

    def __init__(self) -> None:
        self._registrations: dict[
            tuple[ComponentKind, str, str], _Registration
        ] = {}

    def register(
        self,
        kind: ComponentKind,
        component_id: str,
        version: str,
        factory: ComponentFactory,
        *,
        validate: ConfigValidator | None = None,
    ) -> None:
        reference = ComponentRef(kind, component_id, version)
        if reference.key in self._registrations:
            raise ValueError(f"duplicate component registration: {component_id}@{version}")
        if not callable(factory) or validate is not None and not callable(validate):
            raise ValueError("component registration callback is invalid")
        self._registrations[reference.key] = _Registration(factory, validate)

    def preflight(self, references: Sequence[ComponentRef]) -> None:
        seen: set[tuple[ComponentKind, str, str, str]] = set()
        for reference in references:
            if not isinstance(reference, ComponentRef):
                raise ValueError("component reference is invalid")
            identity = (
                *reference.key,
                json.dumps(dict(reference.config), sort_keys=True, separators=(",", ":")),
            )
            if identity in seen:
                raise ValueError(
                    f"duplicate component reference: {reference.id}@{reference.version}"
                )
            seen.add(identity)
            registration = self._registrations.get(reference.key)
            if registration is None:
                raise ValueError(
                    f"unknown trusted component: {reference.kind.value}/"
                    f"{reference.id}@{reference.version}"
                )
            if registration.validate is not None:
                registration.validate(reference.config)

    def resolve(self, reference: ComponentRef) -> object:
        self.preflight((reference,))
        registration = self._registrations[reference.key]
        try:
            return registration.factory(reference.config)
        except Exception as error:
            raise ValueError(
                f"trusted component construction failed: {reference.id}@{reference.version}"
            ) from error

    def describe(self) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "kind": kind.value,
                "id": component_id,
                "version": version,
            }
            for kind, component_id, version in sorted(
                self._registrations, key=lambda item: tuple(str(value) for value in item)
            )
        )


__all__ = [
    "ComponentCatalog",
    "ComponentFactory",
    "ComponentKind",
    "ComponentRef",
    "ConfigValidator",
    "MAX_COMPONENT_CONFIG_BYTES",
]
