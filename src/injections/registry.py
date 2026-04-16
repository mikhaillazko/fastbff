from collections.abc import Callable
from collections.abc import Iterable
from functools import lru_cache
from functools import wraps
from typing import Annotated
from typing import Any

from fastapi import Depends

from . import resolver
from .dependant import Dependant
from .dependency_context_manager import DependencyContextManager
from .dependency_provider import DependencyProvider


class InjectorRegistry:
    """Owns the DI graph for a process.

    Use :meth:`inject` to wrap a callable so its ``Annotated[..., Depends(...)]``
    parameters are resolved from the active scope, and :meth:`entrypoint` to
    wrap a top-level callable that opens a fresh resolution scope for itself
    and any ``inject``-wrapped callables it transitively invokes.
    """

    def __init__(self) -> None:
        self._dependency_context = DependencyContextManager()
        self._dependency_provider = DependencyProvider()
        self._registry: dict[Callable, Dependant] = {}

    @property
    def dependency_provider(self) -> DependencyProvider:
        return self._dependency_provider

    @property
    def dependency_context(self) -> DependencyContextManager:
        return self._dependency_context

    def inject(self, func: Callable) -> Callable:
        """Wrap *func* so its ``Depends(...)`` parameters are auto-resolved.

        Must be called from within an active :meth:`entrypoint` scope (or any
        ``self.dependency_context.init_context()`` block).
        """
        dependant = Dependant(call=func)
        self._registry[func] = dependant

        @wraps(func)
        def decorator(*args: Any, **kwargs: Any) -> Any:
            dependency_cache, stack = self._dependency_context.get_context()
            resolved_dependencies, errors, _ = resolver.solve_dependencies(
                dependant=dependant,
                dependency_cache=dependency_cache,
                stack=stack,
                dependency_provider=self._dependency_provider,
            )
            if errors:
                raise ValueError(errors)

            return dependant.call(*args, **kwargs, **resolved_dependencies)

        return decorator

    def entrypoint(self, func: Callable) -> Callable:
        """Wrap *func* as a top-level entrypoint that opens its own DI scope."""
        dependant = Dependant(call=func)
        self._registry[func] = dependant

        @wraps(func)
        def decorator(*args: Any, **kwargs: Any) -> Any:
            with self._dependency_context.init_context() as (dependency_cache, stack):
                resolved_dependencies, errors, _ = resolver.solve_dependencies(
                    dependant=dependant,
                    dependency_cache=dependency_cache,
                    stack=stack,
                    dependency_provider=self._dependency_provider,
                )
                if errors:
                    raise ValueError(errors)

                return dependant.call(*args, **kwargs, **resolved_dependencies)

        return decorator

    def resolve[T](self, type_: type[T]) -> Iterable[T]:
        with self._dependency_context.init_context() as (dependency_cache, stack):
            resolved_dependency, errors, _ = resolver.solve_dependency(
                type_=type_,
                dependency_cache=dependency_cache,
                stack=stack,
                dependency_provider=self._dependency_provider,
            )
            if errors:
                raise ValueError(errors)
            yield resolved_dependency


@lru_cache
def get_injector_registry() -> InjectorRegistry:
    return InjectorRegistry()


IInjectorRegistry = Annotated[InjectorRegistry, Depends(get_injector_registry)]
