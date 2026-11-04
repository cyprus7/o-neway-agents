"""Explicit host composition. Factories return resources owned by app lifespan."""
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any


@dataclass(frozen=True)
class AgentDependencies:
    repository_factory: Callable[..., Any] | None = None
    model_factory: Callable[[Any, Any], Any] | None = None
    travel_data_factory: Callable[[Any, Any], Any] | None = None
    identity_resolver: Callable[[Any], Any] | None = None
