"""Tests for ``QueryExecutor.fetch`` — call-level and entity-level caching."""

from dataclasses import dataclass
from inspect import signature
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


class FetchTenantEntitiesQuery(Query[dict[int, Entity]]):
    ids: frozenset[int]
    tenant_id: int


# ---------------------------------------------------------------------------
# fetch() — call-level caching
# ---------------------------------------------------------------------------


def test_fetch_call_level_caches(app, query_executor) -> None:
    # Arrange
    spy = MagicMock(side_effect=lambda request: PlainResult(value=request.key))

    @app.queries
    def fetch_plain(query_args: FetchPlainQuery) -> PlainResult:
        return spy(request=query_args)

    # Act
    result_1 = query_executor.fetch(FetchPlainQuery(key='a'))
    result_2 = query_executor.fetch(FetchPlainQuery(key='a'))

    # Assert
    assert result_1 == result_2
    spy.assert_called_once()


def test_fetch_different_query_fields_each_fetched(app, query_executor) -> None:
    # Arrange
    spy = MagicMock(side_effect=lambda request: PlainResult(value=request.key))

    @app.queries
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


def test_fetch_entity_first_call_fetches_all(app, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    # Act
    result = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    # Assert
    assert set(result.keys()) == {1, 2, 3}
    spy.assert_called_once()


def test_fetch_entity_same_ids_not_refetched(app, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    spy.reset_mock()

    # Act
    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    # Assert
    spy.assert_not_called()


def test_fetch_entity_subset_not_refetched(app, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    spy.reset_mock()

    # Act
    result = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2})))

    # Assert
    assert set(result.keys()) == {1, 2}
    spy.assert_not_called()


def test_fetch_entity_overlapping_fetches_only_missing(app, query_executor) -> None:
    # Arrange
    spy = _entity_spy()

    @app.queries
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))
    spy.reset_mock()

    # Act
    result = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({2, 3, 4})))

    # Assert
    assert set(result.keys()) == {2, 3, 4}
    spy.assert_called_once_with(ids=frozenset({4}))


def test_fetch_absent_ids_excluded_from_result(app, query_executor) -> None:
    # Arrange
    spy = MagicMock(return_value={})

    @app.queries
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        return spy(ids=query_args.ids)

    # Act
    result = query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1, 2, 3})))

    # Assert
    assert result == {}
    spy.assert_called_once()


def test_fetch_absent_ids_not_refetched_on_overlap(app, query_executor) -> None:
    # Arrange — backend only returns id 1; ids 2 and 3 are absent.
    spy = MagicMock(side_effect=lambda ids: {i: Entity(value=f'e:{i}') for i in ids if i == 1})

    @app.queries
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


def test_fetch_absent_id_becomes_present_in_new_executor(app, query_executor) -> None:
    # Arrange — absence is cached per-executor (per-request); a new executor must re-fetch.
    call_args: list[frozenset[int]] = []

    @app.queries
    def fetch_entities(query_args: FetchEntitiesQuery) -> dict[int, Entity]:
        call_args.append(query_args.ids)
        return {}

    query_executor.fetch(FetchEntitiesQuery(ids=frozenset({1})))

    # Act
    fresh_executor = QueryExecutor.create(query_annotations=app.query_annotations)
    fresh_executor.fetch(FetchEntitiesQuery(ids=frozenset({1})))

    # Assert
    assert len(call_args) == 2


def test_fetch_entity_discriminating_field_does_not_share_bucket(app, query_executor) -> None:
    """An entity query with a field beyond its ids (e.g. ``tenant_id``) must not
    cross-serve cached entries between different values of that field."""
    # Arrange — the backend tags each entity with the tenant it was fetched for.
    seen: list[tuple[int, frozenset[int]]] = []

    @app.queries
    def fetch_tenant_entities(query_args: FetchTenantEntitiesQuery) -> dict[int, Entity]:
        seen.append((query_args.tenant_id, query_args.ids))
        return {i: Entity(value=f't{query_args.tenant_id}:{i}') for i in query_args.ids}

    # Act — same ids, different tenants, within one request/executor.
    tenant_1 = query_executor.fetch(FetchTenantEntitiesQuery(ids=frozenset({1, 2}), tenant_id=1))
    tenant_2 = query_executor.fetch(FetchTenantEntitiesQuery(ids=frozenset({1, 2}), tenant_id=2))

    # Assert — each tenant is fetched independently and gets its own entities.
    assert tenant_1 == {1: Entity(value='t1:1'), 2: Entity(value='t1:2')}
    assert tenant_2 == {1: Entity(value='t2:1'), 2: Entity(value='t2:2')}
    assert seen == [(1, frozenset({1, 2})), (2, frozenset({1, 2}))]


def test_fetch_entity_same_discriminating_field_shares_bucket(app, query_executor) -> None:
    """Within the same discriminating value, overlapping id sets still share the
    entity cache — only missing ids are fetched."""
    # Arrange
    spy = MagicMock(side_effect=lambda tenant_id, ids: {i: Entity(value=f't{tenant_id}:{i}') for i in ids})

    @app.queries
    def fetch_tenant_entities(query_args: FetchTenantEntitiesQuery) -> dict[int, Entity]:
        return spy(tenant_id=query_args.tenant_id, ids=query_args.ids)

    query_executor.fetch(FetchTenantEntitiesQuery(ids=frozenset({1, 2, 3}), tenant_id=1))
    spy.reset_mock()

    # Act — same tenant, overlapping ids.
    result = query_executor.fetch(FetchTenantEntitiesQuery(ids=frozenset({2, 3, 4}), tenant_id=1))

    # Assert — only the new id 4 hits the backend.
    assert set(result.keys()) == {2, 3, 4}
    spy.assert_called_once_with(tenant_id=1, ids=frozenset({4}))


def test_query_executor_has_empty_signature() -> None:
    """Endpoints declare ``Annotated[QueryExecutor, Depends(QueryExecutor)]``, so
    FastAPI introspects ``inspect.signature(QueryExecutor)`` at startup. The
    parameterless ``__init__`` must keep that signature empty — otherwise
    ``__init__`` params would leak in as request fields. Guards the invariant
    that replaced the former ``QueryExecutor.__signature__ = Signature([])``
    mutation.
    """
    assert list(signature(QueryExecutor).parameters) == []
