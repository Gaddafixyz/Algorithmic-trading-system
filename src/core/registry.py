"""
Component registry for runtime component lookup.

Provides a type-safe dictionary of named components.
Components register themselves during startup via the DI container.
"""

from __future__ import annotations

import logging
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ComponentRegistry:
    """Thread-safe registry mapping names to component instances."""

    def __init__(self) -> None:
        self._components: dict[str, Any] = {}

    def register(self, name: str, component: Any, *, overwrite: bool = False) -> None:
        """Register a component by name."""
        if name in self._components and not overwrite:
            raise ValueError(f"Component '{name}' already registered. Use overwrite=True to replace.")
        self._components[name] = component
        logger.debug("Registered component: %s (%s)", name, type(component).__name__)

    def get(self, name: str, component_type: type[T]) -> T:
        """Retrieve a component by name with type checking."""
        component = self._components.get(name)
        if component is None:
            available = ", ".join(self._components.keys()) or "none"
            raise KeyError(f"Component '{name}' not registered. Available: {available}")
        if not isinstance(component, component_type):
            raise TypeError(
                f"Component '{name}' is {type(component).__name__}, "
                f"expected {component_type.__name__}"
            )
        return component

    def get_optional(self, name: str, component_type: type[T]) -> T | None:
        """Retrieve a component by name, returning None if not found."""
        component = self._components.get(name)
        if component is None:
            return None
        if not isinstance(component, component_type):
            raise TypeError(
                f"Component '{name}' is {type(component).__name__}, "
                f"expected {component_type.__name__}"
            )
        return component

    def unregister(self, name: str) -> bool:
        """Remove a component. Returns True if it existed."""
        if name in self._components:
            del self._components[name]
            logger.debug("Unregistered component: %s", name)
            return True
        return False

    def list_registered(self) -> list[str]:
        """Return all registered component names."""
        return list(self._components.keys())

    def clear(self) -> None:
        """Remove all registrations (use in tests only)."""
        self._components.clear()

    def __contains__(self, name: str) -> bool:
        return name in self._components

    def __len__(self) -> int:
        return len(self._components)


_registry: ComponentRegistry | None = None


def get_registry() -> ComponentRegistry:
    """Return the singleton component registry."""
    global _registry  # noqa: PLW0603
    if _registry is None:
        _registry = ComponentRegistry()
    return _registry


def reset_registry() -> None:
    """Reset the singleton — for testing only."""
    global _registry  # noqa: PLW0603
    _registry = None
