import types as builtin_types
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any
from typing import TypeGuard
from typing import Union
from typing import get_args
from typing import get_origin
from typing import get_type_hints

from fastbff.exceptions import QueryRegistrationError

from .query import EntityQuery
from .query import Query


def _strip_none(t: Any) -> Any:
    """Remove NoneType from a simple Optional/Union for stable cache key construction."""
    origin = get_origin(t)
    if origin is Union or isinstance(t, builtin_types.UnionType):
        non_none = [a for a in get_args(t) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return t


def _is_query_subclass(annotation: Any) -> TypeGuard[type[Query]]:
    """Check whether *annotation* is a concrete subclass of :class:`Query`."""
    try:
        return isinstance(annotation, type) and issubclass(annotation, Query) and annotation is not Query
    except TypeError:
        return False


def extract_query_return_type(query_cls: type) -> Any | None:
    """Extract ``T`` from ``Query[T]`` via ``__query_return_type__`` set at class definition time."""
    return getattr(query_cls, '__query_return_type__', None)


def _is_row_shaped(t: Any) -> bool:
    """Whether *t* is a 'rows' shape: ``list[Mapping]``, ``Mapping``, or close.

    The render path lets a handler honestly declare ``-> list[dict[str, Any]]``
    (or single ``Mapping``) and have the framework validate to ``Query[T].T``
    at dispatch time. Anything else has to match ``Query[T].T`` exactly so
    genuine model-mismatch bugs (handler returns ``Entity`` while query says
    ``PlainResult``) still fail at registration.
    """
    import collections.abc as collections_abc

    if t is dict or t is collections_abc.Mapping:
        return True
    origin = get_origin(t)
    if origin is dict or origin is collections_abc.Mapping:
        return True
    if origin is list:
        args = get_args(t)
        if not args:
            return False
        return _is_row_shaped(args[0])
    return isinstance(t, type) and issubclass(t, collections_abc.Mapping)


def _find_ids_field(query_cls: type, key_type: Any) -> str | None:
    """Find the field holding the requested ids on an :class:`EntityQuery` subclass.

    Prefers a field literally named ``ids`` (the documented convention); falls
    back to the unique field typed as an iterable of the dict's key type.
    """
    fields = query_cls.model_fields  # type: ignore[attr-defined]
    if 'ids' in fields:
        return 'ids'
    for field_name, field_info in fields.items():
        field_type = field_info.annotation
        if field_type is None:
            continue
        origin = get_origin(field_type)
        if origin is not None:
            try:
                if issubclass(origin, Iterable):
                    args = get_args(field_type)
                    if args and args[0] == key_type:
                        return field_name
            except TypeError:
                continue
    return None


class QueryAnnotation:
    """Metadata gathered once when a ``@queries`` function is registered.

    Stores the handler and all derived type metadata so that lookups in
    :class:`QueryExecutor` need no further reflection: whether the query opts
    into entity-level caching (an :class:`EntityQuery` subclass) and, lazily,
    whether its result model needs the render pipeline.
    """

    def __init__(self, original_func: Callable, explicit_query_type: type[Query] | None = None) -> None:
        self.original_func = original_func
        hints = get_type_hints(original_func)
        return_type = hints.get('return')
        if return_type is None:
            raise QueryRegistrationError(
                f'@queries {original_func.__name__!r}: handler must declare a return type annotation.',
            )
        self.return_type: type = return_type

        # Detect a Query[T] parameter; an explicit_query_type from the decorator
        # (``@queries(SomeQueryType)``) covers parameterless handlers.
        self.query_type: type | None = explicit_query_type
        self.query_param_name: str | None = None
        for param_name, param_type in hints.items():
            if param_name == 'return':
                continue
            if _is_query_subclass(param_type):
                if self.query_param_name is not None:
                    raise QueryRegistrationError(
                        f'@queries {original_func.__name__}: multiple Query parameters '
                        f'({self.query_param_name}: {self.query_type.__name__ if self.query_type else "?"}, '
                        f'{param_name}: {param_type.__name__})',
                    )
                if explicit_query_type is not None and explicit_query_type is not param_type:
                    raise QueryRegistrationError(
                        f'@queries {original_func.__name__}: explicit query type '
                        f'{explicit_query_type.__name__} does not match signature parameter '
                        f'{param_name}: {param_type.__name__}',
                    )
                self.query_type = param_type
                self.query_param_name = param_name

        if self.query_type is not None:
            expected_return = extract_query_return_type(self.query_type)
            if (
                expected_return is not None
                and self.return_type != expected_return
                and not _is_row_shaped(self.return_type)
            ):
                raise QueryRegistrationError(
                    f'@queries {original_func.__name__}: return type {self.return_type} '
                    f'does not match {self.query_type.__name__}[{expected_return}]',
                )

        # Entity-level caching is explicit opt-in: only EntityQuery subclasses.
        self.is_entity: bool = False
        self.entity_key_type: Any = None
        self.entity_value_type: Any = None
        self.ids_field: str | None = None
        if (
            self.query_type is not None
            and isinstance(self.query_type, type)
            and issubclass(self.query_type, EntityQuery)
        ):
            dict_return = extract_query_return_type(self.query_type)
            if get_origin(dict_return) is dict:
                key_type, value_type = get_args(dict_return)
                self.entity_key_type = key_type
                self.entity_value_type = _strip_none(value_type)
                self.ids_field = _find_ids_field(self.query_type, key_type)
                if self.ids_field is None:
                    raise QueryRegistrationError(
                        f'EntityQuery {self.query_type.__name__!r} must declare an ids field — a '
                        f'field (conventionally named `ids`) typed as an iterable of {key_type}.',
                    )
                self.is_entity = True

        # Lazy render classification — a model referenced as a return type may
        # not be fully constructed when the decorator fires (forward refs).
        self._render_cache: tuple[Any, ...] = ()

    @property
    def render_target(self) -> tuple[str, Any] | None:
        """Whether handler results should go through :func:`fastbff.resolve.render`.

        Source of truth is ``Query[T].T`` — the *output* contract — so a handler
        can honestly declare ``-> list[dict[str, Any]]`` while the framework
        validates to ``Model`` at the dispatch boundary. ``None`` for entity
        queries, primitives, and models without ``Resolve`` fields. Cached.
        """
        if not self._render_cache:
            from fastbff.resolve import classify_render

            target = extract_query_return_type(self.query_type) if self.query_type is not None else None
            if target is None:
                target = self.return_type
            self._render_cache = (classify_render(target),)
        return self._render_cache[0]

    def __repr__(self) -> str:
        func_name = getattr(self.original_func, '__name__', str(self.original_func))
        return f'QueryAnnotation({self.return_type}, {func_name})'
