from typing import Any
from typing import ClassVar

from pydantic import BaseModel


class Query[T](BaseModel):
    """Typed query object. ``T`` is the return type of the registered handler.

    The return type is recovered from Pydantic's own
    ``__pydantic_generic_metadata__`` and exposed as
    :attr:`__query_return_type__` on each concrete subclass — no module-level
    state, no ``id()``-keyed registries.
    """

    __query_return_type__: ClassVar[Any] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        return_type = _resolve_query_return_type(cls)
        if return_type is not None:
            cls.__query_return_type__ = return_type


def _resolve_query_return_type(cls: type) -> Any:
    """Walk the MRO looking for a parametrized ``Query[T]`` and return ``T``.

    Uses Pydantic's ``__pydantic_generic_metadata__['args']`` populated when
    ``Query[...]`` is subscripted. Returns ``None`` for the base ``Query`` itself
    or for un-parametrised subclasses.
    """
    for base in cls.__mro__:
        metadata = getattr(base, '__pydantic_generic_metadata__', None)
        if not metadata:
            continue
        args = metadata.get('args')
        if args:
            return args[0]
    return None
