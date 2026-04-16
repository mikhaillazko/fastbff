"""Tests for ``QueriesRegistry`` — registration semantics and type checking."""

from dataclasses import dataclass

import pytest

from fastbff.exceptions import QueryRegistrationError
from fastbff.query_executor.query import Query


@dataclass(frozen=True)
class PlainResult:
    value: str


@dataclass(frozen=True)
class Entity:
    value: str


class FetchPlainQuery(Query[PlainResult]):
    key: str


def test_query_type_registered_in_registry(query_registry) -> None:
    # Arrange
    @query_registry
    def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        return PlainResult(value=query_args.key)

    # Act
    annotation = query_registry.get_annotation_by_query_type(FetchPlainQuery)

    # Assert
    assert annotation is not None
    assert annotation.query_type is FetchPlainQuery


def test_return_type_mismatch_raises(query_registry) -> None:
    # Arrange & Act & Assert
    with pytest.raises(QueryRegistrationError, match='return type.*does not match'):

        @query_registry
        def fetch_plain(query_args: FetchPlainQuery) -> Entity:
            return Entity(value='wrong')


def test_function_signature_query_registered(query_registry) -> None:
    @query_registry
    def fetch_plain(key: str) -> PlainResult:
        return PlainResult(value=key)

    annotation = query_registry.get_annotation_by_func(fetch_plain)
    assert annotation.query_type is None
    assert annotation.return_type is PlainResult
