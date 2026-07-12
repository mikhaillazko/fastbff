from collections.abc import Callable
from collections.abc import Mapping
from functools import partial
from inspect import iscoroutinefunction
from typing import Any
from typing import Self

import anyio.from_thread
import anyio.to_thread

from fastbff.exceptions import QueryNotRegisteredError
from fastbff.exceptions import ResolveRegistrationError

from .query import Query
from .query_annotation import QueryAnnotation
from .query_cache import QueryCache


class QueryExecutor:
    """Per-request, **async-native** dispatcher.

    :meth:`fetch` is a coroutine. It dispatches typed query objects with
    automatic caching:

    - Call-level for plain ``Query[T]`` return types.
    - Entity-level for :class:`EntityQuery` — overlapping id sets share cached
      entries, only missing ids are fetched.

    Async handlers run **directly on the event loop**; sync handlers are
    offloaded to an anyio worker thread (:meth:`_call`), so a request-scoped
    resource is touched off-loop only while the sync handler runs. When a
    query's result model declares :class:`~fastbff.resolve.Resolve` fields,
    :meth:`fetch` runs the render pipeline (plan → concurrent fetch → merge).

    The executor also carries the resolved dependency map for every registered
    handler / resolver. Dependencies are resolved once per request by FastAPI
    and handed to :meth:`create`; dispatch is a dict lookup.

    ``__init__`` is intentionally parameterless so an endpoint can declare
    ``Annotated[QueryExecutor, Depends(QueryExecutor)]`` and FastAPI sees an
    empty signature; the mount-time override supplies the real instance. Build a
    populated executor with :meth:`create`. Sync endpoints inject
    :class:`SyncQueryExecutor` instead.
    """

    def __init__(self) -> None:
        self._query_annotations: Mapping[type, QueryAnnotation] = {}
        self._cache = QueryCache()
        self._resolved_deps: dict[str, Any] = {}
        self._handler_index: dict[Callable, dict[str, Any]] = {}

    @classmethod
    def create(
        cls,
        query_annotations: Mapping[type, QueryAnnotation],
        *,
        resolved_deps: dict[str, Any] | None = None,
        handler_index: dict[Callable, dict[str, Any]] | None = None,
    ) -> Self:
        """Build a populated executor.

        This is the canonical constructor for both ``provide_query_executor``
        and tests. ``__init__`` stays parameterless so the class keeps an empty
        FastAPI-facing signature (see the class docstring).
        """
        executor = cls()
        executor._query_annotations = query_annotations
        executor._resolved_deps = resolved_deps or {}
        executor._handler_index = handler_index or {}
        return executor

    def deps_for(self, func: Callable) -> dict[str, Any]:
        """Return the resolved kwargs map for *func* (handler or resolver).

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

    async def _call(self, func: Callable, /, *args: Any, **kwargs: Any) -> Any:
        """Invoke a handler / resolver, bridging on the correct thread.

        Async callables are awaited directly on the loop (no worker thread, so
        async composition never exhausts the pool). Sync callables are offloaded
        to a single anyio worker thread via :func:`anyio.to_thread.run_sync`,
        preserving thread affinity for whatever request-scoped resource they
        touch.
        """
        if iscoroutinefunction(func):
            return await func(*args, **kwargs)
        return await anyio.to_thread.run_sync(partial(func, *args, **kwargs))

    async def fetch[T](self, query_obj: Query[T]) -> T:
        query_type = type(query_obj)
        annotation = self._query_annotations.get(query_type)
        if annotation is None:
            raise QueryNotRegisteredError(f'No @queries registered for query object {query_type}')

        handler = annotation.original_func
        extra_kwargs = self.deps_for(handler)
        query_param_name = annotation.query_param_name
        query_kwargs = {query_param_name: query_obj} if query_param_name is not None else {}

        if annotation.is_entity:
            ids_field = annotation.ids_field
            # Both guaranteed non-None by QueryAnnotation for entity queries.
            assert ids_field is not None
            assert query_param_name is not None
            param_name = query_param_name
            ids = frozenset(getattr(query_obj, ids_field))
            # The entity bucket must be keyed by everything on the query *except*
            # the ids field — otherwise two queries that differ only in a
            # discriminating field (e.g. ``tenant_id``) but share ids would
            # collide in one bucket and cross-serve entries.
            discriminators = {name: value for name, value in dict(query_obj).items() if name != ids_field}
            bucket_key = self._cache.build_key(handler, discriminators, annotation.entity_value_type)

            async def entity_fetcher(missing: frozenset[Any]) -> dict[Any, Any]:
                return await self._call(
                    handler,
                    **{param_name: query_obj.model_copy(update={ids_field: missing})},
                    **extra_kwargs,
                )

            result = await self._cache.get_or_fetch_entities(bucket_key, ids, entity_fetcher)
            return result  # type: ignore[return-value]

        cache_key = self._cache.build_key(handler, dict(query_obj))
        render_info = annotation.render_target

        async def call_fetcher() -> Any:
            result = await self._call(handler, **query_kwargs, **extra_kwargs)
            if render_info is not None:
                from fastbff.resolve import apply_render

                return await apply_render(result, render_info, self)
            return result

        return await self._cache.get_or_call(cache_key, call_fetcher)

    async def resolve_ids(self, resolve: Any, ids: frozenset[Any]) -> dict[Any, Any]:
        """Fetch the ``dict[key, value]`` backing a :class:`~fastbff.resolve.Resolve` field.

        Called by the render pipeline in phase 2. For the resolver form, invoke
        the resolver with the id-set plus its injected deps; for the query form,
        construct and fetch the target :class:`EntityQuery`.
        """
        if resolve.resolver is not None:
            deps = self.deps_for(resolve.resolver)
            return await self._call(resolve.resolver, ids, **deps)

        query_type = resolve.query_type
        annotation = self._query_annotations.get(query_type)
        if annotation is None or not annotation.is_entity or annotation.ids_field is None:
            name = getattr(query_type, '__name__', query_type)
            raise ResolveRegistrationError(
                f'Resolve({name}) targets a query that is not a registered EntityQuery. '
                'A Resolve(QueryType) field must point at an EntityQuery so its ids field '
                'can be populated with the collected keys.',
            )
        query = query_type(**{annotation.ids_field: ids})
        return await self.fetch(query)


class SyncQueryExecutor:
    """Sync facade over an async :class:`QueryExecutor` for **sync** endpoints.

    A sync FastAPI endpoint runs in a Starlette worker thread; :meth:`fetch`
    bridges the async executor onto the event loop via
    :func:`anyio.from_thread.run`. From there async handlers run loop-native and
    sync handlers are offloaded to worker threads, exactly as for an async
    endpoint. The cost is one loop round-trip per top-level ``fetch``.

    ``__init__`` is parameterless so ``Depends(SyncQueryExecutor)`` presents an
    empty FastAPI-facing signature; the mount-time override supplies the bound
    instance. Build one with :meth:`create`.
    """

    def __init__(self) -> None:
        self._inner: QueryExecutor | None = None

    @classmethod
    def create(cls, inner: QueryExecutor) -> Self:
        facade = cls()
        facade._inner = inner
        return facade

    def fetch[T](self, query_obj: Query[T]) -> T:
        if self._inner is None:
            raise RuntimeError(
                'SyncQueryExecutor is not bound to a QueryExecutor. Inject it with '
                'Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)] on a mounted app, '
                'or build one with SyncQueryExecutor.create(executor).',
            )
        return anyio.from_thread.run(self._inner.fetch, query_obj)
