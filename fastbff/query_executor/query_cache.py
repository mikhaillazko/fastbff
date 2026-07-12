import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import asdict
from dataclasses import is_dataclass
from typing import Any

from pydantic import BaseModel

from fastbff.exceptions import CacheKeyError


class _Missing:
    __slots__ = ()


MISSING = _Missing()


class QueryCache:
    """Dual-level result cache scoped to a single request.

    - Call-level   : memoises exact ``(func, kwargs)`` pairs.
    - Entity-level : stores individual ``dict[K, V]`` entries so overlapping id
                     sets (e.g. ``{1,2,3}`` then ``{2,3,4}``) only fetch the
                     missing ids.

    The cache is **loop-native**: every method is a coroutine and all mutations
    happen on the event loop between ``await`` points, so no thread lock is
    needed. Concurrent identical fetches deduplicate — the call cache shares a
    single in-flight :class:`asyncio.Future` per key, and the entity cache
    serialises per bucket with an :class:`asyncio.Lock` — so a backend query is
    never issued twice for the same key/bucket within a request.
    """

    def __init__(self) -> None:
        self._call_cache: dict[tuple, Any] = {}
        self._call_inflight: dict[tuple, asyncio.Future] = {}
        self._entity_cache: dict[tuple, dict[Any, Any]] = {}
        self._entity_locks: dict[tuple, asyncio.Lock] = {}

    def build_key(self, func: Any, kwargs: dict[str, Any], *extra: Any) -> tuple:
        return (func, *extra, frozenset((k, _to_hashable(v)) for k, v in kwargs.items()))

    async def get_or_call(self, key: tuple, fetcher: Callable[[], Awaitable[Any]]) -> Any:
        """Return the cached result for *key*, awaiting *fetcher* only on a miss.

        Concurrent callers for the same key share one in-flight fetch: the first
        creates a :class:`asyncio.Future`, later callers await it. The future is
        resolved (or failed) once the fetch completes, so a backend call for a
        given key happens at most once per request.
        """
        if key in self._call_cache:
            return self._call_cache[key]
        inflight = self._call_inflight.get(key)
        if inflight is not None:
            return await inflight

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._call_inflight[key] = future
        try:
            result = await fetcher()
        except BaseException as exc:
            self._call_inflight.pop(key, None)
            if not future.done():
                future.set_exception(exc)
            future.exception()  # mark retrieved — awaiters still re-raise via await
            raise
        else:
            self._call_cache[key] = result
            self._call_inflight.pop(key, None)
            if not future.done():
                future.set_result(result)
            return result

    async def get_or_fetch_entities(
        self,
        bucket_key: tuple,
        ids: frozenset[Any],
        fetcher: Callable[[frozenset[Any]], Awaitable[dict[Any, Any]]],
    ) -> dict[Any, Any]:
        """Return a mapping for the requested ids, fetching only those not yet cached.

        Serialised per bucket with an :class:`asyncio.Lock` so two overlapping
        requests never fetch the same missing id twice. Holding an asyncio lock
        across the awaited fetch is safe — it is cooperative, not a thread lock.
        """
        lock = self._entity_locks.setdefault(bucket_key, asyncio.Lock())
        async with lock:
            entity_map = self._entity_cache.setdefault(bucket_key, {})
            missing = frozenset(ids - entity_map.keys())
            if missing:
                fetched = await fetcher(missing)
                for key, value in fetched.items():
                    entity_map.setdefault(key, value)
                for id_ in missing:
                    entity_map.setdefault(id_, MISSING)  # Mark as "checked but absent"
            return {id_: entity_map[id_] for id_ in ids if entity_map.get(id_, MISSING) is not MISSING}


def _to_hashable(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return tuple(_to_hashable(i) for i in v)
    if isinstance(v, (set, frozenset)):
        return frozenset(_to_hashable(i) for i in v)
    if isinstance(v, dict):
        return frozenset((k, _to_hashable(val)) for k, val in v.items())
    if isinstance(v, BaseModel):
        return _to_hashable(v.model_dump(mode='python'))
    if is_dataclass(v) and not isinstance(v, type):
        return _to_hashable(asdict(v))
    try:
        hash(v)
    except TypeError as exc:
        type_name = type(v).__name__
        raise CacheKeyError(
            f'Cannot build a cache key from a {type_name!r} value on a Query field: '
            'it is not hashable. fastbff caches query results by their arguments, so every '
            'Query field must be hashable or a shape fastbff can normalise (lists, tuples, '
            'sets, dicts, pydantic models, dataclasses). Make '
            f'{type_name!r} hashable — e.g. a frozen dataclass or '
            '`model_config = ConfigDict(frozen=True)` — or drop it from the Query.',
        ) from exc
    return v
