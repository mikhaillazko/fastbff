"""Tests for cache-key construction — especially the hashable normalisation
of common Pydantic / dataclass shapes that used to crash ``build_key``."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import pytest
from pydantic import BaseModel

from fastbff.exceptions import CacheKeyError
from fastbff.query_executor.query_cache import QueryCache
from fastbff.query_executor.query_cache import _to_hashable


def _handler() -> None: ...


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
