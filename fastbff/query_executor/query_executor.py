from collections.abc import Callable
from collections.abc import Iterable
from typing import Any

from .query import Query
from .query_cache import QueryCache
from .registry import IQueriesRegistry


class QueryExecutor:
    """Per-request executor.

    :meth:`fetch` dispatches typed query objects with automatic caching:

    - Call-level for plain return types.
    - Entity-level for ``dict[K, V]`` queries with an IDs field:
      overlapping id sets share cached entries, only missing ids are
      fetched from the underlying query.
    """

    def __init__(self, queries_registry: IQueriesRegistry) -> None:
        self._queries_registry = queries_registry
        self._cache = QueryCache()

    def fetch[T](self, query_obj: Query[T]) -> T:
        annotation = self._queries_registry.get_annotation_by_query_type(type(query_obj))
        query_param_name = annotation.query_param_name
        assert query_param_name is not None

        if annotation.dict_type_key is not None:
            ids_field = annotation.ids_param_name
            if ids_field is not None:
                ids_value = getattr(query_obj, ids_field, None)
                if isinstance(ids_value, Iterable) and not isinstance(ids_value, (str, bytes)):
                    ids = frozenset(ids_value)
                    bucket_key = self._cache.build_key(annotation.call, {}, annotation.dict_value_type)
                    result = self._cache.get_or_fetch_entities(
                        bucket_key,
                        ids,
                        lambda missing: annotation.call(
                            **{query_param_name: query_obj.model_copy(update={ids_field: missing})},
                        ),
                    )
                    return result  # type: ignore[return-value]

        cache_key = self._cache.build_key(
            annotation.call,
            dict(query_obj),
            annotation.dict_value_type if annotation.dict_type_key is not None else None,
        )
        return self._cache.get_or_call(cache_key, lambda: annotation.call(**{query_param_name: query_obj}))

    def call[T](self, handler: Callable[..., T], /, **kwargs: Any) -> T:
        """Function-signature dispatch: call a registered ``@queries``-decorated function with caching.

        The same call-level and entity-level cache layers used by :meth:`fetch`
        apply here::

            @queries
            def fetch_users(ids: frozenset[int]) -> dict[int, User]: ...

            users = executor.call(fetch_users, ids=frozenset({1, 2, 3}))
        """
        annotation = self._queries_registry.get_annotation_by_func(handler)

        if annotation.dict_type_key is not None and annotation.ids_param_name is not None:
            ids_param_name = annotation.ids_param_name
            ids_value = kwargs.get(ids_param_name)
            if isinstance(ids_value, Iterable) and not isinstance(ids_value, (str, bytes)):
                ids = frozenset(ids_value)
                shared_kwargs = {k: v for k, v in kwargs.items() if k != ids_param_name}
                bucket_key = self._cache.build_key(
                    annotation.call,
                    shared_kwargs,
                    annotation.dict_value_type,
                )
                result = self._cache.get_or_fetch_entities(
                    bucket_key,
                    ids,
                    lambda missing: annotation.call(**shared_kwargs, **{ids_param_name: missing}),
                )
                return result  # type: ignore[return-value]

        cache_key = self._cache.build_key(
            annotation.call,
            kwargs,
            annotation.dict_value_type if annotation.dict_type_key is not None else None,
        )
        return self._cache.get_or_call(cache_key, lambda: annotation.call(**kwargs))
