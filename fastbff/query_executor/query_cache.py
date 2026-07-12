import threading
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

    Thread-safe: under ``QueryExecutor.afetch`` several fetches can run on
    worker threads concurrently (e.g. ``asyncio.gather``). Cache *mutations*
    are guarded by a lock, but the ``fetcher`` callable runs **outside** the
    lock — it may bridge a coroutine onto the event loop, and holding the lock
    across that would risk a worker↔loop deadlock. Concurrent fetches of the
    same key may therefore both run; the first result wins (``setdefault``),
    which is consistent because handlers for a given query are idempotent.
    """

    def __init__(self) -> None:
        self._call_cache: dict[tuple, Any] = {}
        self._entity_cache: dict[tuple, dict[Any, Any]] = {}
        self._lock = threading.RLock()

    def build_key(self, func: Any, kwargs: dict[str, Any], *extra: Any) -> tuple:
        return (func, *extra, frozenset((k, _to_hashable(v)) for k, v in kwargs.items()))

    def get_or_call(self, key: tuple, fetcher: Callable[[], Any]) -> Any:
        with self._lock:
            if key in self._call_cache:
                return self._call_cache[key]
        result = fetcher()
        with self._lock:
            return self._call_cache.setdefault(key, result)

    def get_or_fetch_entities(
        self,
        bucket_key: tuple,
        ids: frozenset[Any],
        fetcher: Callable[[frozenset[Any]], dict[Any, Any]],
    ) -> dict[Any, Any]:
        """Return a mapping for the requested ids, fetching only those not yet cached."""
        with self._lock:
            entity_map = self._entity_cache.setdefault(bucket_key, {})
            missing = frozenset(ids - entity_map.keys())
        if missing:
            fetched = fetcher(missing)
            with self._lock:
                for key, value in fetched.items():
                    entity_map.setdefault(key, value)
                for id_ in missing:
                    entity_map.setdefault(id_, MISSING)  # Mark as "checked but absent"
        with self._lock:
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
