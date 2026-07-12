"""Tests for ``QueryRouter`` registration semantics and type checking."""

import asyncio
from dataclasses import dataclass

import pytest

from fastbff import Query
from fastbff.exceptions import QueryRegistrationError


@dataclass(frozen=True)
class _PlainResult:
    value: str


@dataclass(frozen=True)
class _Entity:
    value: str


class _FetchPlainQuery(Query[_PlainResult]):
    key: str


class _FetchAllEntities(Query[list[_Entity]]):
    pass


def test_query_type_registered_in_app(app) -> None:
    # Arrange
    @app.queries
    def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        return _PlainResult(value=query_args.key)

    # Act
    annotation = app.get_annotation_by_query_type(_FetchPlainQuery)

    # Assert
    assert annotation is not None
    assert annotation.query_type is _FetchPlainQuery


def test_return_type_mismatch_raises(query_router) -> None:
    # Arrange & Act & Assert
    with pytest.raises(QueryRegistrationError, match='return type.*does not match'):

        @query_router.queries
        def fetch_plain(query_args: _FetchPlainQuery) -> _Entity:
            return _Entity(value='wrong')


def test_parameterless_handler_bound_via_decorator_factory(app) -> None:
    # Arrange
    @app.queries(_FetchAllEntities)
    def fetch_all_entities() -> list[_Entity]:
        return [_Entity(value='a'), _Entity(value='b')]

    # Act
    annotation = app.get_annotation_by_query_type(_FetchAllEntities)

    # Assert
    assert annotation.query_type is _FetchAllEntities
    assert annotation.query_param_name is None


def test_parameterless_fetch_via_query_executor(app, query_executor) -> None:
    # Arrange
    @app.queries(_FetchAllEntities)
    def fetch_all_entities() -> list[_Entity]:
        return [_Entity(value='a'), _Entity(value='b')]

    # Act
    result = asyncio.run(query_executor.fetch(_FetchAllEntities()))

    # Assert
    assert result == [_Entity(value='a'), _Entity(value='b')]


def test_explicit_query_type_mismatch_raises(query_router) -> None:
    # Arrange & Act & Assert
    with pytest.raises(QueryRegistrationError, match='explicit query type.*does not match'):

        @query_router.queries(_FetchAllEntities)
        def mismatched(query_args: _FetchPlainQuery) -> _PlainResult:
            return _PlainResult(value=query_args.key)


def test_router_raises_on_duplicate_query_function(query_router) -> None:
    """Re-registering the same function as a query on a single router raises."""

    # Arrange
    def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        return _PlainResult(value=query_args.key)

    query_router.queries(fetch_plain)

    # Act & Assert
    with pytest.raises(QueryRegistrationError, match='Duplicate @queries registration'):
        query_router.queries(fetch_plain)


def test_async_query_handler_registers(query_router) -> None:
    """`async def` handlers register fine — they are bridged at fetch time via afetch."""

    @query_router.queries
    async def fetch_plain(query_args: _FetchPlainQuery) -> _PlainResult:
        return _PlainResult(value=query_args.key)

    assert fetch_plain in query_router._query_func_annotations_registry
