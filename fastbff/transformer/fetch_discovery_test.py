"""Tests for the static fetch-target discovery + finalize-time validation."""

from typing import Annotated

import pytest
from fastapi import Depends

from fastbff import BatchArg
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff.exceptions import TransformerRegistrationError
from fastbff.transformer.fetch_discovery import discover_fetched_queries


class _FetchUsers(Query[dict[int, str]]):
    ids: frozenset[int]


class _FetchTeams(Query[list[str]]):
    pass


# discover_fetched_queries — recognised idioms ---------------------------------


def test_discovers_canonical_fetch_call() -> None:
    def transformer_fn(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> str | None:
        return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    assert discover_fetched_queries(transformer_fn, query_executor_type=QueryExecutor) == {_FetchUsers}


def test_executor_param_identified_by_annotation_not_name() -> None:
    """The parameter named ``qx`` (not ``query_executor``) is still picked up."""

    def transformer_fn(
        owner_id: int,
        batch: BatchArg[int],
        qx: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> str | None:
        return qx.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    assert discover_fetched_queries(transformer_fn, query_executor_type=QueryExecutor) == {_FetchUsers}


def test_discovers_multiple_fetch_calls() -> None:
    def transformer_fn(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> str | None:
        users = query_executor.fetch(_FetchUsers(ids=batch.ids))
        teams = query_executor.fetch(_FetchTeams())
        return users.get(owner_id) or (teams[0] if teams else None)

    assert discover_fetched_queries(transformer_fn, query_executor_type=QueryExecutor) == {
        _FetchUsers,
        _FetchTeams,
    }


def test_discovers_through_single_assignment_name_binding() -> None:
    def transformer_fn(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> str | None:
        query = _FetchUsers(ids=batch.ids)
        return query_executor.fetch(query).get(owner_id)

    assert discover_fetched_queries(transformer_fn, query_executor_type=QueryExecutor) == {_FetchUsers}


def test_discovers_query_class_from_closure_cell() -> None:
    def make_transformer():
        local_query_cls = _FetchUsers

        def transformer_fn(
            owner_id: int,
            batch: BatchArg[int],
            query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
        ) -> str | None:
            return query_executor.fetch(local_query_cls(ids=batch.ids)).get(owner_id)

        return transformer_fn

    transformer_fn = make_transformer()
    assert discover_fetched_queries(transformer_fn, query_executor_type=QueryExecutor) == {_FetchUsers}


# discover_fetched_queries — silent misses (no false positives) ---------------


def test_silently_skips_when_name_is_reassigned() -> None:
    """Multiple bindings of the same local — refuse to guess which is live."""

    def transformer_fn(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> str | None:
        query = _FetchUsers(ids=batch.ids)
        query = _FetchTeams()  # type: ignore[assignment]
        return query_executor.fetch(query)  # type: ignore[return-value]

    assert discover_fetched_queries(transformer_fn, query_executor_type=QueryExecutor) == set()


def test_silently_skips_self_attribute_executor() -> None:
    """``self.qe.fetch(...)`` — receiver is Attribute, not Name. Documented miss."""

    class _Service:
        def __init__(
            self,
            query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
        ) -> None:
            self.query_executor = query_executor

        def transformer_fn(self, owner_id: int, batch: BatchArg[int]) -> str | None:
            return self.query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    assert discover_fetched_queries(_Service.transformer_fn, query_executor_type=QueryExecutor) == set()


def test_returns_empty_when_no_executor_param() -> None:
    def transformer_fn(owner_id: int, batch: BatchArg[int]) -> str:
        return str(owner_id)

    assert discover_fetched_queries(transformer_fn, query_executor_type=QueryExecutor) == set()


# finalize-time validation -----------------------------------------------------


def test_finalize_raises_when_transformer_fetches_unregistered_query(app: FastBFF) -> None:
    @app.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> str | None:
        return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    with pytest.raises(TransformerRegistrationError, match=r'fetches query types.*_FetchUsers'):
        app.finalize()


def test_finalize_succeeds_when_fetch_target_is_registered(app: FastBFF) -> None:
    @app.queries
    def fetch_users(args: _FetchUsers) -> dict[int, str]:
        return {i: f'user-{i}' for i in args.ids}

    @app.transformer
    def transform_owner(
        owner_id: int,
        batch: BatchArg[int],
        query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
    ) -> str | None:
        return query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    app.finalize()


def test_finalize_silent_miss_does_not_block_unrelated_transformer(app: FastBFF) -> None:
    """A transformer whose fetch target the validator can't see (self-attribute)
    must not be reported as missing — best-effort, no false positives."""

    class _Service:
        def __init__(
            self,
            query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
        ) -> None:
            self.query_executor = query_executor

        def transformer_fn(self, owner_id: int, batch: BatchArg[int]) -> str | None:
            return self.query_executor.fetch(_FetchUsers(ids=batch.ids)).get(owner_id)

    app.transformer(_Service.transformer_fn)
    app.finalize()
