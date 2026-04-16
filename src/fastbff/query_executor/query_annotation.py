import types as builtin_types
from collections.abc import Callable
from collections.abc import Iterable
from typing import Any
from typing import Union
from typing import get_args
from typing import get_origin
from typing import get_type_hints

from .query import Query


def _strip_none(t: Any) -> Any:
    """Remove NoneType from a simple Optional/Union for stable cache key construction."""
    origin = get_origin(t)
    if origin is Union or isinstance(t, builtin_types.UnionType):
        non_none = [a for a in get_args(t) if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return t


def _find_ids_param(hints: dict[str, Any], key_type: type) -> str | None:
    """Find the parameter typed as ``Iterable[K]`` matching the dict's key type."""
    for param_name, param_type in hints.items():
        if param_name == 'return':
            continue
        origin = get_origin(param_type)
        if origin is not None:
            try:
                if issubclass(origin, Iterable):
                    args = get_args(param_type)
                    if args and args[0] == key_type:
                        return param_name
            except TypeError:
                continue
    return None


def _is_query_subclass(annotation: Any) -> bool:
    """Check whether *annotation* is a concrete subclass of :class:`Query`."""
    try:
        return isinstance(annotation, type) and issubclass(annotation, Query) and annotation is not Query
    except TypeError:
        return False


def extract_query_return_type(query_cls: type) -> Any | None:
    """Extract ``T`` from ``Query[T]`` via ``__query_return_type__`` set at class definition time."""
    return getattr(query_cls, '__query_return_type__', None)


def _find_ids_field_on_query(query_cls: type, key_type: type) -> str | None:
    """Find a field on the query class typed as ``Iterable[K]`` matching the dict's key type."""
    for field_name, field_info in query_cls.model_fields.items():  # type: ignore[attr-defined]
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
    """Metadata gathered once when a ``@query`` function is registered.

    Stores the injected callable and all derived type metadata so that
    lookups in :class:`QueryExecutor` need no further reflection.
    """

    def __init__(self, call: Callable, original_func: Callable) -> None:
        self.call = call
        self.original_func = original_func
        hints = get_type_hints(original_func)
        self.return_type: type | None = hints.get('return')

        # Detect Query[T] parameter
        self.query_type: type | None = None
        self.query_param_name: str | None = None
        for param_name, param_type in hints.items():
            if param_name == 'return':
                continue
            if _is_query_subclass(param_type):
                if self.query_type is not None:
                    func_name = getattr(original_func, '__name__', str(original_func))
                    raise TypeError(
                        f'@query {func_name}: multiple Query parameters '
                        f'({self.query_param_name}: {self.query_type.__name__}, '
                        f'{param_name}: {param_type.__name__})',
                    )
                self.query_type = param_type
                self.query_param_name = param_name

        # Pre-compute dict[K, V] metadata; None if return type is not a dict.
        self.dict_value_type: Any = None
        self.dict_type_key: tuple[type, Any] | None = None
        self.ids_param_name: str | None = None
        origin = get_origin(self.return_type)
        if origin is not None and issubclass(origin, dict):
            key_type, value_type = get_args(self.return_type)
            self.dict_value_type = _strip_none(value_type)
            self.dict_type_key = (key_type, self.dict_value_type)
            # For query-object handlers, look for IDs field on the query class
            if self.query_type is not None:
                self.ids_param_name = _find_ids_field_on_query(self.query_type, key_type)
            else:
                self.ids_param_name = _find_ids_param(hints, key_type)

    def __repr__(self) -> str:
        func_name = getattr(self.original_func, '__name__', str(self.original_func))
        return f'QueryAnnotation({self.return_type}, {func_name})'
