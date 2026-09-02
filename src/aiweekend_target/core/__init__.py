"""Universal agent core. Concrete domains live in trusted adapters."""

from aiweekend_target.core.contracts import (
    Analyzer,
    AttackTransform,
    Boundary,
    BoundaryContext,
    BoundaryEvidence,
    ControlAction,
    ControlDecision,
    ControlPolicy,
    Evaluator,
    Finding,
    ModelDescriptor,
    ModelInvocation,
    ModelProvider,
    TransformOutcome,
)
from aiweekend_target.core.catalog import (
    ComponentCatalog,
    ComponentKind,
    ComponentRef,
)
from aiweekend_target.core.pipeline import (
    AllowPolicy,
    BoundaryPipeline,
    BoundaryPipelineError,
    FindingPolicy,
)


__all__ = [
    "AllowPolicy",
    "Analyzer",
    "AttackTransform",
    "Boundary",
    "BoundaryContext",
    "BoundaryEvidence",
    "BoundaryPipeline",
    "BoundaryPipelineError",
    "ComponentCatalog",
    "ComponentKind",
    "ComponentRef",
    "ControlAction",
    "ControlDecision",
    "ControlPolicy",
    "Evaluator",
    "Finding",
    "FindingPolicy",
    "ModelDescriptor",
    "ModelInvocation",
    "ModelProvider",
    "TransformOutcome",
]
