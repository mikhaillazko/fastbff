from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated
from typing import Any
from typing import get_origin

from .dependant import Dependant
from .dependencies_setup import DependenciesSetup
from .reflection import get_dependency_callable

DependencyConfiguration = dict[Callable[..., Any], Callable[..., Any]]


class DependencyProvider:
    dependency_overrides: DependencyConfiguration

    def __init__(self) -> None:
        self.dependency_overrides = {}

    def init(self, dependency_setup: DependenciesSetup) -> None:
        for dependency_type, dependency_impl in dependency_setup:
            dependency_type = self._get_real_origin(dependency_type)
            self.dependency_overrides[dependency_type] = dependency_impl

    def get_dependant_impl(self, original: Callable[..., Any]) -> Dependant:
        call = get_dependency_callable(original)
        if call in self.dependency_overrides:
            call = self.dependency_overrides[call]
        return Dependant(call=call)

    @contextmanager
    def override(
        self,
        original: Callable[..., Any],
        override: Callable[..., Any],
    ) -> Iterator[None]:
        original_call = get_dependency_callable(original)
        source = self.dependency_overrides.get(original_call)
        self.dependency_overrides[original_call] = override
        yield
        if source is not None:
            self.dependency_overrides[original_call] = source
        else:
            self.dependency_overrides.pop(original_call, None)

    def _get_real_origin[T](self, type_or_annotated: Callable[..., T] | type[T]) -> Callable[..., T] | type[T]:
        """Also can accept class annotated by ``Annotated[Class, Depends(ClassOrFunc)]``."""
        if get_origin(type_or_annotated) is Annotated:
            return type_or_annotated.__origin__  # type: ignore[attr-defined]
        return type_or_annotated
