from collections.abc import Iterator
from contextlib import ExitStack
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .types import DependenciesCache


@dataclass(frozen=True)
class Scope:
    stack: ExitStack
    cache: DependenciesCache


_scope_context: ContextVar[Scope | None] = ContextVar('scope_context', default=None)


class DependencyContextManager:
    @contextmanager
    def init_context(self) -> Iterator[tuple[DependenciesCache, ExitStack]]:
        scope = Scope(stack=ExitStack(), cache={})
        token = _scope_context.set(scope)
        try:
            yield scope.cache, scope.stack
            scope.stack.close()
        except BaseException as exc:
            scope.stack.__exit__(type(exc), exc, exc.__traceback__)
            raise
        finally:
            _scope_context.reset(token)

    def get_context(self) -> tuple[DependenciesCache, ExitStack]:
        scope = _scope_context.get()
        if scope is None:
            raise RuntimeError('get_context() called outside of an active init_context() scope')
        return scope.cache, scope.stack
