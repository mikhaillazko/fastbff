from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from inspect import iscoroutine
from inspect import iscoroutinefunction
from typing import Any
from typing import Self

import anyio.from_thread
import anyio.to_thread

from fastbff.exceptions import AsyncDispatchError
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

    ``__init__`` is intentionally parameterless: an endpoint declares
    ``Annotated[QueryExecutor, Depends(QueryExecutor)]``, so FastAPI
    introspects ``inspect.signature(QueryExecutor)`` at startup. A
    parameterless constructor presents an empty signature naturally — no
    ``__init__`` params leak in as request fields, and at request time the
    ``QueryExecutor → provide_query_executor`` override supplies the real
    instance. Build a populated executor with :meth:`create`.
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
        and tests. ``__init__`` stays parameterless so the class keeps an
        empty FastAPI-facing signature (see the class docstring).
        """
        executor = cls()
        executor._query_annotations = query_annotations
        executor._resolved_deps = resolved_deps or {}
        executor._handler_index = handler_index or {}
        return executor

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

    def call_handler(self, func: Callable, /, *args: Any, **kwargs: Any) -> Any:
        """Invoke a handler or transformer, bridging ``async def`` callables to the loop.

        Sync callables run inline. For an ``async def`` callable we submit its
        coroutine to the host event loop with :func:`anyio.from_thread.run`,
        which blocks the *current worker thread* — never the loop — until it
        resolves. This is the same primitive Starlette uses, so it works inside
        both a sync FastAPI endpoint (which Starlette already runs in a worker
        thread) and :meth:`afetch`, and on asyncio or trio backends alike.

        Raises :class:`AsyncDispatchError` when there is no worker-thread portal
        to bridge through: a purely synchronous ``fetch`` reached an async
        handler, or an async handler called sync ``fetch`` from the loop thread
        (which would self-deadlock — use ``await afetch`` there instead). A
        ``RuntimeError`` raised by the coroutine *itself* is left to propagate
        unchanged, distinguished by the ``bridged`` flag.
        """
        if not iscoroutinefunction(func):
            result = func(*args, **kwargs)
            if iscoroutine(result):
                # func slipped past iscoroutinefunction — e.g. an object with an
                # ``async def __call__`` or an async callable behind a wrapper
                # that doesn't preserve the coroutine code flags. Returning it
                # would cache/return an unawaited coroutine, the exact silent
                # corruption this bridge exists to prevent. Fail loudly instead.
                result.close()
                name = getattr(func, '__name__', repr(func))
                raise AsyncDispatchError(
                    f'{name!r} returned a coroutine but was not recognised as an async callable '
                    'by inspect.iscoroutinefunction, so fastbff cannot bridge it. Declare it as a '
                    'plain `async def` handler/transformer (not a wrapped or __call__-based async '
                    'callable) so async dispatch can detect and await it.',
                )
            return result

        bridged = False

        async def run_on_loop() -> Any:
            nonlocal bridged
            bridged = True
            return await func(*args, **kwargs)

        try:
            return anyio.from_thread.run(run_on_loop)
        except RuntimeError as error:
            if bridged:
                raise  # the coroutine itself raised — not a bridging failure
            name = getattr(func, '__name__', repr(func))
            raise AsyncDispatchError(
                f'async {name!r} was reached on a path that cannot await it. Call it through '
                'a sync FastAPI endpoint (Starlette runs sync endpoints in a worker thread, '
                'where fastbff bridges the coroutine onto the loop) or via '
                '`await query_executor.afetch(...)` from an async endpoint. Inside an async '
                'handler, use `await query_executor.afetch(...)` for further fetches rather '
                'than sync `fetch`.',
            ) from error

    async def afetch[T](self, query_obj: Query[T]) -> T:
        """Async entry point for :meth:`fetch` that supports ``async def`` handlers.

        Runs the (synchronous) :meth:`fetch` machinery on a worker thread via
        :func:`anyio.to_thread.run_sync`; any ``async def`` handler or
        transformer reached during that fetch is bridged back onto the loop by
        :meth:`call_handler`. Use this from ``async def`` FastAPI endpoints.

        A **sync** endpoint does not need this — Starlette already runs sync
        endpoints in a worker thread, so it can call :meth:`fetch` directly and
        async handlers still bridge.
        """
        return await anyio.to_thread.run_sync(self.fetch, query_obj)

    def fetch[T](self, query_obj: Query[T]) -> T:
        # Late import — fastbff.batch depends on QueryExecutor; importing it
        # at module top would create a cycle.
        from fastbff.batch import apply_auto_wrap

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
                    # The entity bucket must be keyed by everything on the query
                    # *except* the ids field — otherwise two queries that differ
                    # only in a discriminating field (e.g. ``tenant_id``) but share
                    # ids would collide in one bucket and cross-serve entries.
                    discriminators = {name: value for name, value in dict(query_obj).items() if name != ids_field}
                    bucket_key = self._cache.build_key(handler, discriminators, annotation.dict_value_type)
                    result = self._cache.get_or_fetch_entities(
                        bucket_key,
                        ids,
                        lambda missing: self.call_handler(
                            handler,
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

        wrap_info = annotation.auto_wrap

        def fetcher() -> Any:
            result = self.call_handler(handler, **query_kwargs, **extra_kwargs)
            if wrap_info is not None:
                return apply_auto_wrap(result, wrap_info, self)
            return result

        return self._cache.get_or_call(cache_key, fetcher)
