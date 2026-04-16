"""Tests for ``QueryExecutor.fetch`` — call-level and entity-level caching."""

from dataclasses import dataclass
from unittest.mock import MagicMock

from fastbff.query_executor.query import Query
from fastbff.query_executor.query_executor import QueryExecutor

# ---------------------------------------------------------------------------
# Shared return types
# (declared at module level so get_type_hints can resolve them in closures)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlainResult:
    value: str


@dataclass(frozen=True)
class Entity:
    value: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entity_spy() -> MagicMock:
    """A spy that returns one Entity per requested id."""
    return MagicMock(side_effect=lambda ids: {i: Entity(value=f'e:{i}') for i in ids})


# ---------------------------------------------------------------------------
# Query objects
# ---------------------------------------------------------------------------


class FetchPlainQuery(Query[PlainResult]):
    key: str


class FetchEntitiesQuery(Query[dict[int, Entity]]):
    ids: frozenset[int]


# ---------------------------------------------------------------------------
# fetch() — call-level caching
# ---------------------------------------------------------------------------


def test_fetch_call_level_caches(query_registry, query_executor) -> None:
    # Arrange
    spy = MagicMock(side_effect=lambda request: PlainResult(value=request.key))

    @query_registry
    def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        return spy(request=query_args)

    # Act
    result_1 = query_executor.fetch(FetchPlainQuery(key='a'))
    result_2 = query_executor.fetch(FetchPlainQuery(key='a'))

    # Assert
    assert result_1 == result_2
    spy.assert_called_once()


def test_fetch_different_query_fields_each_fetched(query_registry, query_executor) -> None:
    # Arrange
    spy = MagicMock(side_effect=lambda request: PlainResult(value=request.key))

    @query_registry
    def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        return spy(request=query_args)

    # Act
    result_1 = query_executor.fetch(FetchPlainQuery(key='a'))
    result_2 = query_executor.fetch(FetchPlainQuery(key='b'))

    # Assert
    assert result_1.value == 'a'
    assert result_2.value == 'b'
    assert spy.call_count == 2


# ---------------------------------------------------------------------------
# fetch() — entity-level caching (dict return + IDs field)
# ---------------------------------------------------------------------------


def test_fetch_entity_first_call_fetches_all(query_registry, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @query_registry
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    # Act
    result = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    # Assert
    assert set(result.keys()) == {1, 2, 3}
    spy.assert_called_once()


def test_fetch_entity_same_ids_not_refetched(query_registry, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @query_registry
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    spy.reset_mock()

    # Act
    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    # Assert
    spy.assert_not_called()


def test_fetch_entity_subset_not_refetched(query_registry, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @query_registry
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    spy.reset_mock()

    # Act
    result = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2})))

    # Assert
    assert set(result.keys()) == {1, 2}
    spy.assert_not_called()


def test_fetch_entity_overlapping_fetches_only_missing(query_registry, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @query_registry
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    spy.reset_mock()

    # Act
    result = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({2, 3, 4})))

    # Assert
    assert set(result.keys()) == {2, 3, 4}
    spy.assert_called_once_with(ids=frozenset({4}))


def test_fetch_absent_ids_excluded_from_result(query_registry, query_executor) -> None:
    # Arrange
    spy = MagicMock(return_value={})

    @query_registry
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    # Act
    result = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    # Assert
    assert result == {}
    spy.assert_called_once()


def test_fetch_absent_ids_not_refetched_on_overlap(query_registry, query_executor) -> None:
    # Arrange — backend only returns id 1; ids 2 and 3 are absent.
    spy = MagicMock(side_effect=lambda ids: {i: Entity(value=f'e:{i}') for i in ids if i == 1})

    @query_registry
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    result_1 = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    assert set(result_1.keys()) == {1}
    assert spy.call_count == 1

    # Act — overlap on absent ids 2 and 3; only the new id 4 should be fetched.
    result_2 = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({2, 3, 4})))

    # Assert
    assert result_2 == {}  # 4 is also absent
    spy.assert_called_with(ids=frozenset({4}))
    assert spy.call_count == 2


def test_fetch_absent_id_becomes_present_in_new_executor(query_registry, query_executor) -> None:
    # Arrange — absence is cached per-executor (per-request); a new executor must re-fetch.
    call_args: list[frozenset[int]] = []

    @query_registry
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        call_args.append(query_args.ids)
        return {}

    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1})))

    # Act
    fresh_executor = QueryExecutor(queries_registry=query_registry)  # type: ignore[arg-type]
    fresh_executor.fetch(FetchEntitiesQuery(ids=frozenset({1})))

    # Assert
    assert len(call_args) == 2


# ---------------------------------------------------------------------------
# call() — function-signature dispatch
# ---------------------------------------------------------------------------


def test_call_function_signature_caches_call_level(query_registry, query_executor) -> None:
    spy = MagicMock(side_effect=lambda key: PlainResult(value=key))

    @query_registry
    def fetch_plain(key: str) -> PlainResult:
        return spy(key)

    a = query_executor.call(fetch_plain, key='a')
    b = query_executor.call(fetch_plain, key='a')

    assert a == b == PlainResult(value='a')
    spy.assert_called_once()


def test_call_function_signature_entity_cache_dedupes(query_registry, query_executor) -> None:
    spy = MagicMock(side_effect=lambda ids: {i: Entity(value=f'e:{i}') for i in ids})

    @query_registry
    def fetch_entities(ids: frozenset[int]) -> dict[int, Entity]:
        return spy(ids)

    query_executor.call(fetch_entities, ids=frozenset({1, 2, 3}))
    spy.reset_mock()

    result = query_executor.call(fetch_entities, ids=frozenset({2, 3, 4}))

    assert set(result.keys()) == {2, 3, 4}
    spy.assert_called_once_with(frozenset({4}))
