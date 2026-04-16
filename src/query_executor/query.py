from typing import Any
from typing import ClassVar

from pydantic import BaseModel

_query_return_types: dict[int, Any] = {}


class Query[T](BaseModel):
    """Typed query object. ``T`` is the return type."""

    __query_return_type__: ClassVar[Any] = None

    def __class_getitem__(cls, params: type[Any] | tuple[type[Any], ...]) -> Any:
        result = super().__class_getitem__(params)
        _query_return_types[id(result)] = params if not isinstance(params, tuple) else params[0]
        return result

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in cls.__mro__:
            return_type = _query_return_types.get(id(base))
            if return_type is not None:
                cls.__query_return_type__ = return_type
                return
