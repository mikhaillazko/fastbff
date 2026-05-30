import pytest

from fastbff import FastBFF
from fastbff.query_executor.query_executor import QueryExecutor
from fastbff.router import QueryRouter


@pytest.fixture()
def app() -> FastBFF:
    return FastBFF()


@pytest.fixture()
def query_router() -> QueryRouter:
    return QueryRouter()


@pytest.fixture()
def query_executor(app: FastBFF) -> QueryExecutor:
    return QueryExecutor.create(query_annotations=app.query_annotations)
