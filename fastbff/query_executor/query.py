from typing import Any
from typing import ClassVar

from pydantic import BaseModel


class Query[T](BaseModel):
    """Typed query object. ``T`` is the return type of the registered handler.

    The return type is recovered from Pydantic's own
    ``__pydantic_generic_metadata__`` and exposed as
    :attr:`__query_return_type__` on each concrete subclass — no module-level
    state, no ``id()``-keyed registries.

    A plain ``Query[T]`` gets **call-level** caching (keyed by handler + args).
    To opt into **entity-level** caching (overlapping id sets share cached
    entries, only missing ids are fetched), subclass :class:`EntityQuery`.
    """

    __query_return_type__: ClassVar[Any] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        return_type = cls._resolve_query_return_type()
        if return_type is not None:
            cls.__query_return_type__ = return_type

    @classmethod
    def _resolve_query_return_type(cls) -> Any:
        """Walk the MRO for a parametrized ``Query[T]`` and return ``T``.

        Uses Pydantic's ``__pydantic_generic_metadata__['args']`` populated when
        ``Query[...]`` is subscripted. Returns ``None`` for the base ``Query``
        itself or for un-parametrised subclasses.
        """
        for base in cls.__mro__:
            metadata = getattr(base, '__pydantic_generic_metadata__', None)
            if not metadata:
                continue
            args = metadata.get('args')
            if args:
                return args[0]
        return None


class EntityQuery[K, V](Query[dict[K, V]]):
    """A query whose handler returns ``dict[K, V]``, opting into entity-level caching.

    Overlapping id sets across a request share cached entries and only the
    missing ids are fetched from the underlying handler; absences are
    remembered, so re-asking does not re-hit the backend. Declare a field
    holding the requested ids (conventionally named ``ids``) typed as an
    iterable of ``K``::

        class FetchUsers(EntityQuery[int, User]):
            ids: frozenset[int]

        @app.queries
        def fetch_users(query: FetchUsers, session: DBSession) -> dict[int, User]:
            ...

    Entity caching is now **explicit** — a plain ``Query[dict[K, V]]`` gets
    call-level caching only, so adding an unrelated iterable field never
    silently changes cache semantics.
    """

    @classmethod
    def _resolve_query_return_type(cls) -> Any:
        for base in cls.__mro__:
            metadata = getattr(base, '__pydantic_generic_metadata__', None)
            if not metadata:
                continue
            args = metadata.get('args')
            if args and len(args) == 2:
                return dict[args[0], args[1]]
        return None
