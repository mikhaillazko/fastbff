from collections.abc import Callable
from typing import Any


class _Missing:
    __slots__ = ()


MISSING = _Missing()


class QueryCache:
    """Dual-level result cache scoped to a single request.

    - Call-level   : memoises exact ``(func, kwargs)`` pairs.
    - Entity-level : stores individual ``dict[K, V]`` entries so overlapping id
                     sets (e.g. ``{1,2,3}`` then ``{2,3,4}``) only fetch the
                     missing ids.
    """

    def __init__(self) -> None:
        self._call_cache: dict[tuple, Any] = {}
        self._entity_cache: dict[tuple, dict[Any, Any]] = {}

    def build_key(self, func: Any, kwargs: dict[str, Any], *extra: Any) -> tuple:
        return (func, *extra, frozenset((k, _to_hashable(v)) for k, v in kwargs.items()))

    def get_or_call(self, key: tuple, fetcher: Callable[[], Any]) -> Any:
        if key not in self._call_cache:
            self._call_cache[key] = fetcher()
        return self._call_cache[key]

    def get_or_fetch_entities(
        self,
        bucket_key: tuple,
        ids: frozenset[Any],
        fetcher: Callable[[frozenset[Any]], dict[Any, Any]],
    ) -> dict[Any, Any]:
        """Return a mapping for the requested ids, fetching only those not yet cached."""
        entity_map = self._entity_cache.setdefault(bucket_key, {})
        missing = ids - entity_map.keys()
        if missing:
            entity_map.update(fetcher(missing))
            for id_ in missing:
                if id_ not in entity_map:
                    entity_map[id_] = MISSING  # Mark as "checked but absent"
        return {id_: entity_map[id_] for id_ in ids if entity_map.get(id_, MISSING) is not MISSING}


def _to_hashable(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return tuple(_to_hashable(i) for i in v)
    if isinstance(v, (set, frozenset)):
        return frozenset(_to_hashable(i) for i in v)
    if isinstance(v, dict):
        return frozenset((k, _to_hashable(val)) for k, val in v.items())
    return v
