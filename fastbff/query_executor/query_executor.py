from collections.abc import Callable
from collections.abc import Iterable
from inspect import Signature
from typing import Any

from fastbff.exceptions import QueryNotRegisteredError

from .query import Query
from .query_annotation import QueryAnnotation
from .query_cache import QueryCache


class QueryExecutor:
    """Per-request executor.

    :meth:`fetch` dispatches typed query objects with automatic caching:

    - Call-level for plain return types.
    - Entity-level for ``dict[K, V]`` queries with an IDs field:
      overlapping id sets share cached entries, only missing ids are
      fetched from the underlying query.

    The executor also carries the resolved dependency map for every
    registered handler (queries + transformers). Dependencies are resolved
    once per request by FastAPI's ``solve_dependencies`` when the endpoint
    asks for the executor via ``Depends(provide_query_executor)``; dispatch
    is a dict lookup.
    """

    def __init__(
        self,
        query_annotations: dict[type, QueryAnnotation],
        *,
        resolved_deps: dict[str, Any] | None = None,
        handler_index: dict[Callable, dict[str, Any]] | None = None,
    ) -> None:
        self._query_annotations = query_annotations
        self._cache = QueryCache()
        self._resolved_deps = resolved_deps or {}
        self._handler_index = handler_index or {}

    def deps_for(self, func: Callable) -> dict[str, Any]:
        """Return the resolved kwargs map for *func* (handler or transformer).

        Any ``QueryExecutor``-typed parameter receives ``self``; other entries
        are looked up in the shared resolved-deps map produced by FastAPI.
        """
        from fastbff.di import QUERY_EXECUTOR_SENTINEL

        per_func = self._handler_index.get(func)
        if not per_func:
            return {}
        out: dict[str, Any] = {}
        for arg_name, slot in per_func.items():
            if slot is QUERY_EXECUTOR_SENTINEL:
                out[arg_name] = self
            else:
                out[arg_name] = self._resolved_deps[slot]
        return out

    def fetch[T](self, query_obj: Query[T]) -> T:
        query_type = type(query_obj)
        annotation = self._query_annotations.get(query_type)
        if annotation is None:
            raise QueryNotRegisteredError(f'No @query registered for query object {query_type}')

        handler = annotation.original_func
        extra_kwargs = self.deps_for(handler)
        query_param_name = annotation.query_param_name
        query_kwargs = {query_param_name: query_obj} if query_param_name is not None else {}

        if annotation.dict_type_key is not None and query_param_name is not None:
            ids_field = annotation.ids_param_name
            if ids_field is not None:
                ids_value = getattr(query_obj, ids_field, None)
                if isinstance(ids_value, Iterable) and not isinstance(ids_value, (str, bytes)):
                    ids = frozenset(ids_value)
                    bucket_key = self._cache.build_key(handler, {}, annotation.dict_value_type)
                    result = self._cache.get_or_fetch_entities(
                        bucket_key,
                        ids,
                        lambda missing: handler(
                            **{query_param_name: query_obj.model_copy(update={ids_field: missing})},
                            **extra_kwargs,
                        ),
                    )
                    return result  # type: ignore[return-value]

        cache_key = self._cache.build_key(
            handler,
            dict(query_obj),
            annotation.dict_value_type if annotation.dict_type_key is not None else None,
        )
        return self._cache.get_or_call(
            cache_key,
            lambda: handler(**query_kwargs, **extra_kwargs),
        )


# FastAPI introspects ``Depends(QueryExecutor)`` by reading
# ``inspect.signature(QueryExecutor)``, which would otherwise expose
# ``__init__`` parameters as request params. The override to
# ``provide_query_executor`` fires at solve time; the empty signature just
# keeps ``get_dependant`` happy.
QueryExecutor.__signature__ = Signature(parameters=[])  # type: ignore[attr-defined]
