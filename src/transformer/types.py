import types as builtin_types
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Union
from typing import get_args
from typing import get_origin

from pydantic import BaseModel as PydanticBaseModel
from pydantic_core.core_schema import ValidationInfo

from src.injections.dependant import cached_signature
from src.injections.reflection import find_arg_info

_TRANSFORMER_ATTR = '__transformer__'
_BATCHES_ATTR = '__batches__'


@dataclass
class BatchInfo:
    field_name: str
    key: str
    batch_fetch_type: Any | None = field(default=None)


class BatchArg[T](PydanticBaseModel):
    ids: frozenset[T]


def _extract_value_type(return_type: type) -> type | None:
    """Extract the concrete item type from Optional[V], list[V], or plain V."""
    origin = get_origin(return_type)
    if origin is Union or isinstance(return_type, builtin_types.UnionType):
        non_none = [a for a in get_args(return_type) if a is not type(None)]
        return non_none[0] if len(non_none) == 1 else None
    if origin is list:
        args = get_args(return_type)
        return args[0] if args else None
    if isinstance(return_type, type):
        return return_type
    return None


def _get_batch_key_type(batch_arg_cls: type) -> type | None:
    """Extract T from a parameterised BatchArg[T] (Pydantic generic model)."""
    metadata = getattr(batch_arg_cls, '__pydantic_generic_metadata__', None)
    if metadata and metadata.get('args'):
        return metadata['args'][0]
    return None


class TransformerAnnotation:
    def __init__(
        self,
        call: Callable,
        return_type: type,
    ):
        batch_arg_name, batch_arg_cls = find_arg_info(call, BatchArg)
        self.batch_arg_name = batch_arg_name
        call_sign = cached_signature(call)
        self.has_info_arg = any(
            param_val.annotation is ValidationInfo for param_key, param_val in call_sign.parameters.items()
        )
        self.call = call
        self.return_type = return_type
        self.batch_fetch_type: type | None = None
        if batch_arg_cls is not None:
            key_type = _get_batch_key_type(batch_arg_cls)
            value_type = _extract_value_type(return_type)
            if key_type is not None and value_type is not None:
                self.batch_fetch_type = dict[key_type, value_type]  # type: ignore[valid-type]

    @property
    def batch_key(self) -> str | None:
        return f'{self.call}#{self.batch_arg_name}' if self.batch_arg_name is not None else None

    def __repr__(self) -> str:
        func_name = getattr(self.call, '__name__', str(self.call))
        return f'TransformerAnnotation({func_name}, batch_arg_name: {self.batch_arg_name})'
