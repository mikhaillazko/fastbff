"""Tests for ``QueryCache`` — the async-native dual-level result cache.

Both cache methods are coroutines, so each scenario is driven with
``asyncio.run(...)`` (no pytest-asyncio plugin required). Cache-key
construction (``build_key`` / ``_to_hashable``) stays synchronous and is
tested directly.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pytest
from pydantic import BaseModel

from fastbff.exceptions import CacheKeyError
from fastbff.query_executor.query_cache import QueryCache
from fastbff.query_executor.query_cache import _to_hashable


def _handler() -> None: ...


# ---------------------------------------------------------------------------
# _to_hashable — normalisation of common Pydantic / dataclass / container shapes
# ---------------------------------------------------------------------------


def test_to_hashable_normalises_nested_containers() -> None:
    value = {'a': [1, 2], 'b': {3, 4}, 'c': {'nested': (5, 6)}}
    result = _to_hashable(value)
    assert _to_hashable(value) == result  # deterministic
    assert isinstance(result, frozenset)


def test_to_hashable_handles_pydantic_model() -> None:
    class _Filter(BaseModel):
        name: str
        tags: list[str]

    one = _to_hashable(_Filter(name='x', tags=['a', 'b']))
    two = _to_hashable(_Filter(name='x', tags=['a', 'b']))
    other = _to_hashable(_Filter(name='y', tags=['a', 'b']))

    assert one == two
    assert one != other
    assert hash(one) == hash(two)


def test_to_hashable_handles_nested_model_inside_container() -> None:
    class _Inner(BaseModel):
        n: int

    value = {'items': [_Inner(n=1), _Inner(n=2)]}
    assert _to_hashable(value) == _to_hashable({'items': [_Inner(n=1), _Inner(n=2)]})


def test_to_hashable_handles_dataclass() -> None:
    @dataclass
    class _Coord:
        lat: float
        lon: float

    one = _to_hashable(_Coord(1.0, 2.0))
    two = _to_hashable(_Coord(1.0, 2.0))
    assert one == two
    assert hash(one) == hash(two)


def test_to_hashable_passes_through_natively_hashable_scalars() -> None:
    stamp = datetime(2026, 5, 30, 12, 0, 0)
    uid = UUID('00000000-0000-0000-0000-000000000001')
    assert _to_hashable(stamp) is stamp
    assert _to_hashable(uid) is uid
    assert _to_hashable('plain') == 'plain'


def test_to_hashable_raises_cache_key_error_for_unhashable() -> None:
    class _Unhashable:
        __hash__ = None  # type: ignore[assignment]

    with pytest.raises(CacheKeyError, match='not hashable'):
        _to_hashable(_Unhashable())


# ---------------------------------------------------------------------------
# build_key
# ---------------------------------------------------------------------------


def test_build_key_uses_model_value_as_part_of_key() -> None:
    class _Filter(BaseModel):
        name: str

    cache = QueryCache()
    key_a = cache.build_key(_handler, {'flt': _Filter(name='a')})
    key_b = cache.build_key(_handler, {'flt': _Filter(name='b')})
    key_a_again = cache.build_key(_handler, {'flt': _Filter(name='a')})

    assert key_a == key_a_again
    assert key_a != key_b


def test_build_key_with_model_does_not_raise() -> None:
    class _Filter(BaseModel):
        when: datetime

    cache = QueryCache()
    key = cache.build_key(_handler, {'flt': _Filter(when=datetime(2026, 1, 1))})
    assert hash(key)  # key is hashable end-to-end


def test_build_key_extra_parts_discriminate() -> None:
    cache = QueryCache()
    key_one = cache.build_key(_handler, {'x': 1}, 'tenant-a')
    key_two = cache.build_key(_handler, {'x': 1}, 'tenant-b')
    assert key_one != key_two


# ---------------------------------------------------------------------------
# get_or_call — call-level memoisation + in-flight dedup
# ---------------------------------------------------------------------------


def test_get_or_call_memoises_on_key() -> None:
    cache = QueryCache()
    calls = 0

    async def fetcher() -> str:
        nonlocal calls
        calls += 1
        return 'result'

    async def scenario() -> tuple[str, str]:
        first = await cache.get_or_call(('k',), fetcher)
        second = await cache.get_or_call(('k',), fetcher)
        return first, second

    first, second = asyncio.run(scenario())

    assert first == second == 'result'
    assert calls == 1  # fetcher ran once, second call served from cache


def test_get_or_call_distinct_keys_each_fetched() -> None:
    cache = QueryCache()
    calls = 0

    async def scenario() -> tuple[str, str]:
        async def make(value: str) -> str:
            nonlocal calls
            calls += 1
            return value

        result_a = await cache.get_or_call(('a',), lambda: make('a'))
        result_b = await cache.get_or_call(('b',), lambda: make('b'))
        return result_a, result_b

    result_a, result_b = asyncio.run(scenario())

    assert result_a == 'a'
    assert result_b == 'b'
    assert calls == 2


def test_get_or_call_concurrent_identical_keys_run_fetcher_once() -> None:
    """Two concurrent ``get_or_call`` for the same key share one in-flight
    future — the fetcher runs at most once."""
    cache = QueryCache()
    calls = 0

    async def fetcher() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)  # keep the fetch in flight while the twin arrives
        return 'once'

    async def scenario() -> list[str]:
        return await asyncio.gather(
            cache.get_or_call(('k',), fetcher),
            cache.get_or_call(('k',), fetcher),
        )

    results = asyncio.run(scenario())

    assert results == ['once', 'once']
    assert calls == 1


def test_get_or_call_failure_is_not_cached() -> None:
    """A raising fetcher propagates and leaves no cached entry, so a retry
    re-runs the fetcher."""
    cache = QueryCache()
    calls = 0

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError('boom')
        return 'recovered'

    async def scenario() -> str:
        with pytest.raises(RuntimeError, match='boom'):
            await cache.get_or_call(('k',), flaky)
        return await cache.get_or_call(('k',), flaky)

    assert asyncio.run(scenario()) == 'recovered'
    assert calls == 2


# ---------------------------------------------------------------------------
# get_or_fetch_entities — partial fetch, absence remembered, per-bucket
# ---------------------------------------------------------------------------


def _entity_fetcher(seen: list[frozenset[int]]) -> object:
    """Return one entity value per requested id, recording each fetched id set."""

    async def fetcher(missing: frozenset[int]) -> dict[int, str]:
        seen.append(missing)
        return {i: f'e:{i}' for i in missing}

    return fetcher


def test_get_or_fetch_entities_first_call_fetches_all() -> None:
    cache = QueryCache()
    seen: list[frozenset[int]] = []

    async def scenario() -> dict[int, str]:
        return await cache.get_or_fetch_entities(('users',), frozenset({1, 2, 3}), _entity_fetcher(seen))

    result = asyncio.run(scenario())

    assert result == {1: 'e:1', 2: 'e:2', 3: 'e:3'}
    assert seen == [frozenset({1, 2, 3})]


def test_get_or_fetch_entities_same_ids_not_refetched() -> None:
    cache = QueryCache()
    seen: list[frozenset[int]] = []
    fetcher = _entity_fetcher(seen)

    async def scenario() -> None:
        await cache.get_or_fetch_entities(('users',), frozenset({1, 2, 3}), fetcher)
        await cache.get_or_fetch_entities(('users',), frozenset({1, 2, 3}), fetcher)

    asyncio.run(scenario())

    assert seen == [frozenset({1, 2, 3})]  # second call hit the cache entirely


def test_get_or_fetch_entities_overlapping_fetches_only_missing() -> None:
    cache = QueryCache()
    seen: list[frozenset[int]] = []
    fetcher = _entity_fetcher(seen)

    async def scenario() -> dict[int, str]:
        await cache.get_or_fetch_entities(('users',), frozenset({1, 2, 3}), fetcher)
        return await cache.get_or_fetch_entities(('users',), frozenset({2, 3, 4}), fetcher)

    result = asyncio.run(scenario())

    assert result == {2: 'e:2', 3: 'e:3', 4: 'e:4'}
    assert seen == [frozenset({1, 2, 3}), frozenset({4})]  # only the missing id 4 refetched


def test_get_or_fetch_entities_absence_is_remembered() -> None:
    """Ids 2/3 have no backing entity; their absence is remembered via MISSING,
    so an overlapping request fetches only the genuinely-new id."""
    cache = QueryCache()
    seen: list[frozenset[int]] = []

    async def fetcher(missing: frozenset[int]) -> dict[int, str]:
        seen.append(missing)
        return {i: f'e:{i}' for i in missing if i not in {2, 3}}

    async def scenario() -> tuple[dict[int, str], dict[int, str]]:
        first = await cache.get_or_fetch_entities(('users',), frozenset({1, 2, 3}), fetcher)
        second = await cache.get_or_fetch_entities(('users',), frozenset({2, 3, 4}), fetcher)
        return first, second

    first, second = asyncio.run(scenario())

    assert first == {1: 'e:1'}  # absent ids excluded from the returned mapping
    assert second == {4: 'e:4'}  # ids 2/3 known-absent, not refetched
    assert seen == [frozenset({1, 2, 3}), frozenset({4})]


def test_get_or_fetch_entities_distinct_buckets_do_not_share() -> None:
    """Different bucket keys keep independent entity maps — no cross-serving."""
    cache = QueryCache()
    seen_a: list[frozenset[int]] = []
    seen_b: list[frozenset[int]] = []

    async def scenario() -> tuple[dict[int, str], dict[int, str]]:
        bucket_a = await cache.get_or_fetch_entities(('a',), frozenset({1, 2}), _entity_fetcher(seen_a))
        bucket_b = await cache.get_or_fetch_entities(('b',), frozenset({1, 2}), _entity_fetcher(seen_b))
        return bucket_a, bucket_b

    bucket_a, bucket_b = asyncio.run(scenario())

    assert bucket_a == {1: 'e:1', 2: 'e:2'}
    assert bucket_b == {1: 'e:1', 2: 'e:2'}
    assert seen_a == [frozenset({1, 2})]  # bucket b's fetch did not satisfy bucket a
    assert seen_b == [frozenset({1, 2})]
