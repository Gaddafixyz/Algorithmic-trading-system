"""
Lightweight dependency injection container.

Supports:
- Singleton bindings (one instance per container)
- Factory bindings (new instance per resolve)
- Async factory support
- Hierarchical containers for testing

Usage:
    container = DIContainer()
    container.bind_singleton(IExchangeClient, BinanceClient, api_key="...", api_secret="...")
    client = container.resolve(IExchangeClient)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DIContainer:
    """Dependency injection container."""

    def __init__(self, parent: "DIContainer | None" = None) -> None:
        self._parent = parent
        self._singletons: dict[type, Any] = {}
        self._singleton_instances: dict[type, Any] = {}
        self._factories: dict[type, Callable[..., Any]] = {}
        self._factory_kwargs: dict[type, dict[str, Any]] = {}

    def bind_singleton(
        self,
        interface: type[T],
        implementation: type[T] | None = None,
        instance: T | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Bind an interface to a singleton.

        Provide either an implementation class + kwargs, or an existing instance.
        """
        if instance is not None:
            self._singleton_instances[interface] = instance
            logger.debug("Bound singleton instance: %s", interface.__name__)
        elif implementation is not None:
            self._singletons[interface] = implementation
            self._factory_kwargs[interface] = kwargs
            logger.debug(
                "Bound singleton class: %s -> %s",
                interface.__name__,
                implementation.__name__,
            )
        else:
            raise ValueError("Provide either implementation class or instance")

    def bind_factory(
        self,
        interface: type[T],
        factory: Callable[..., T],
        **kwargs: Any,
    ) -> None:
        """Bind an interface to a factory (new instance each resolve)."""
        self._factories[interface] = factory
        self._factory_kwargs[interface] = kwargs
        logger.debug("Bound factory: %s", interface.__name__)

    def resolve(self, interface: type[T]) -> T:
        """
        Resolve an interface to its implementation.

        Raises KeyError if no binding found.
        """
        # Check for pre-built singleton instance
        if interface in self._singleton_instances:
            return self._singleton_instances[interface]  # type: ignore[return-value]

        # Check singleton class bindings
        if interface in self._singletons:
            if interface not in self._singleton_instances:
                impl = self._singletons[interface]
                kwargs = self._factory_kwargs.get(interface, {})
                instance = impl(**kwargs)
                self._singleton_instances[interface] = instance
                logger.debug("Instantiated singleton: %s", interface.__name__)
            return self._singleton_instances[interface]  # type: ignore[return-value]

        # Check factory bindings
        if interface in self._factories:
            factory = self._factories[interface]
            kwargs = self._factory_kwargs.get(interface, {})
            return factory(**kwargs)  # type: ignore[return-value]

        # Check parent container
        if self._parent is not None:
            return self._parent.resolve(interface)

        raise KeyError(
            f"No binding found for {interface.__name__}. "
            f"Registered: {[t.__name__ for t in self._all_bindings()]}"
        )

    async def resolve_async(self, interface: type[T]) -> T:
        """
        Resolve an interface, awaiting async factories if needed.
        """
        # Check for pre-built singleton instance
        if interface in self._singleton_instances:
            return self._singleton_instances[interface]  # type: ignore[return-value]

        if interface in self._singletons:
            if interface not in self._singleton_instances:
                impl = self._singletons[interface]
                kwargs = self._factory_kwargs.get(interface, {})
                instance = impl(**kwargs)
                if asyncio.iscoroutine(instance):
                    instance = await instance
                elif hasattr(instance, "__aenter__"):
                    pass  # Caller handles context manager
                self._singleton_instances[interface] = instance
            return self._singleton_instances[interface]  # type: ignore[return-value]

        if interface in self._factories:
            factory = self._factories[interface]
            kwargs = self._factory_kwargs.get(interface, {})
            if inspect.iscoroutinefunction(factory):
                return await factory(**kwargs)  # type: ignore[return-value]
            return factory(**kwargs)  # type: ignore[return-value]

        if self._parent is not None:
            return await self._parent.resolve_async(interface)

        raise KeyError(f"No binding found for {interface.__name__}")

    def is_registered(self, interface: type) -> bool:
        """Check if an interface has a binding."""
        return (
            interface in self._singletons
            or interface in self._singleton_instances
            or interface in self._factories
            or (self._parent is not None and self._parent.is_registered(interface))
        )

    def create_child(self) -> "DIContainer":
        """Create a child container that falls back to this one."""
        return DIContainer(parent=self)

    def _all_bindings(self) -> list[type]:
        return list(self._singletons.keys()) + list(self._singleton_instances.keys()) + list(self._factories.keys())

    def clear(self) -> None:
        """Clear all bindings — use in tests only."""
        self._singletons.clear()
        self._singleton_instances.clear()
        self._factories.clear()
        self._factory_kwargs.clear()


_container: DIContainer | None = None


def get_container() -> DIContainer:
    """Return the global DI container."""
    global _container  # noqa: PLW0603
    if _container is None:
        _container = DIContainer()
    return _container


def reset_container() -> None:
    """Reset the global container — for testing."""
    global _container  # noqa: PLW0603
    _container = None
