from collections.abc import Callable

import pytest

from fastbff.query_executor.query_executor import QueryExecutor
from fastbff.query_executor.registry import QueriesRegistry


class NoopInjector:
    """Pass functions through unchanged — for tests that don't exercise DI."""

    def inject(self, func: Callable) -> Callable:
        return func


@pytest.fixture()
def noop_injector() -> NoopInjector:
    return NoopInjector()


@pytest.fixture()
def query_registry(noop_injector: NoopInjector) -> QueriesRegistry:
    return QueriesRegistry(injector=noop_injector)  # type: ignore[arg-type]


@pytest.fixture()
def query_executor(query_registry: QueriesRegistry) -> QueryExecutor:
    return QueryExecutor(queries_registry=query_registry)  # type: ignore[arg-type]
