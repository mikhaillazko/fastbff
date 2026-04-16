from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel as PydanticBaseModel

from pydantic_bff.exceptions import RegistrationError

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

    :meth:`render` is the one-call front-door: it does Plan → Fetch → Merge
    for a Pydantic model in a single line.
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

    def render[M: PydanticBaseModel](
        self,
        model: type[M],
        rows: Sequence[Mapping[str, Any]],
    ) -> list[M]:
        """One-call Plan → Fetch → Merge.

        Each ``@transformer(prefetch=...)`` field on *model* contributes one
        bulk query per page. Models without prefetch wiring still work — Phase 2
        is skipped for those batches and you must orchestrate the prefetch
        yourself.
        """
        # Phase 1 — Plan
        from pydantic_bff.transformer.batcher import get_model_batches
        from pydantic_bff.transformer.batcher import populate_context_with_batch

        context = populate_context_with_batch(model, rows)
        # Phase 2 — Fetch
        for batch in get_model_batches(model):
            prefetch = batch.prefetch_query
            if prefetch is None:
                continue
            ids = frozenset(context.get(batch.key, ()))
            self._run_prefetch(prefetch, ids)
        # Phase 3 — Merge
        return [model.model_validate(row, context=context) for row in rows]

    def _run_prefetch(self, prefetch: Any, ids: frozenset[Any]) -> None:
        if isinstance(prefetch, type) and issubclass(prefetch, Query):
            self.fetch(_instantiate_prefetch(prefetch, ids))
            return
        if callable(prefetch):
            annotation = self._queries_registry.get_annotation_by_func(prefetch)
            ids_field = annotation.ids_param_name
            if ids_field is None:
                raise RegistrationError(
                    f'Cannot prefetch via {prefetch!r}: the registered handler has no Iterable parameter '
                    'to bind batch ids to.',
                )
            self.call(prefetch, **{ids_field: ids})
            return
        raise RegistrationError(
            f'prefetch={prefetch!r} must be a Query subclass or a registered @queries function.',
        )


def _instantiate_prefetch(query_cls: type, ids: frozenset[Any]) -> Query[Any]:
    """Build a ``Query`` instance for prefetch given the collected batch ids.

    The query class must declare exactly one ``Iterable``-typed field (the
    standard ``ids: frozenset[T]`` pattern).
    """
    if not (isinstance(query_cls, type) and issubclass(query_cls, Query)):
        raise RegistrationError(
            f'prefetch={query_cls!r} is not a Query subclass; '
            'pass a `Query[...]` subclass to `@transformer(prefetch=...)`.',
        )
    ids_field = _find_iterable_field(query_cls)
    if ids_field is None:
        raise RegistrationError(
            f'Cannot prefetch {query_cls.__name__}: no Iterable field found to bind batch ids to. '
            'Declare e.g. `ids: frozenset[int]` on the Query subclass.',
        )
    return query_cls(**{ids_field: ids})


def _find_iterable_field(query_cls: type) -> str | None:
    from typing import get_args
    from typing import get_origin

    for name, info in query_cls.model_fields.items():  # type: ignore[attr-defined]
        annotation = info.annotation
        origin = get_origin(annotation)
        if origin is None:
            continue
        try:
            if issubclass(origin, Iterable) and get_args(annotation):
                return name
        except TypeError:
            continue
    return None
