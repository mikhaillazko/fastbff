import types as builtin_types
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Union
from typing import get_args
from typing import get_origin

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema
from pydantic_core.core_schema import ValidationInfo

from fastbff.exceptions import BatchContextMissingError
from fastbff.injections.dependant import cached_signature
from fastbff.injections.reflection import find_arg_info

_BATCHES_ATTR = '__batches__'
_FIELD_INFO_ATTR = '_transformer_field_info'


@dataclass
class BatchInfo:
    field_name: str
    key: str
    batch_fetch_type: Any | None = field(default=None)


@dataclass(frozen=True, slots=True)
class BatchArg[BatchKey]:
    """Carrier for the full set of batch keys for a transformer field on the current page.

    Cheap by design: a frozen, slotted dataclass — not a Pydantic model.
    Construct directly as ``BatchArg(ids=frozenset({1, 2, 3}))``.
    """

    ids: frozenset[BatchKey]


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
    """Extract ``T`` from a parameterised ``BatchArg[T]`` (typing.Generic alias)."""
    args = get_args(batch_arg_cls)
    if args:
        return args[0]
    return None


class TransformerFieldInfo:
    """Annotated-metadata + introspection bundle for a ``@transformer`` field.

    A single object that doubles as:

    * **Pydantic field metadata** — implements
      :meth:`__get_pydantic_core_schema__` so it can be placed inside
      ``Annotated[ReturnType, ...]`` and Pydantic will run the wrapped
      transformer as a plain validator.
    * **Introspection metadata** — :func:`introspect_model_transformers` looks
      for instances of this class to discover batch fields.
    """

    def __init__(
        self,
        original_func: Callable,
        wrapped_call: Callable,
        return_type: type,
    ) -> None:
        batch_arg_name, batch_arg_cls = find_arg_info(original_func, BatchArg)
        self.batch_arg_name = batch_arg_name
        call_sign = cached_signature(original_func)
        self.has_info_arg = any(param.annotation is ValidationInfo for param in call_sign.parameters.values())
        self.original_func = original_func
        self.call = wrapped_call
        self.return_type = return_type
        self.batch_fetch_type: type | None = None
        if batch_arg_cls is not None:
            key_type = _get_batch_key_type(batch_arg_cls)
            value_type = _extract_value_type(return_type)
            if key_type is not None and value_type is not None:
                self.batch_fetch_type = dict[key_type, value_type]  # type: ignore[valid-type]

    @property
    def batch_key(self) -> str | None:
        if self.batch_arg_name is None:
            return None
        return f'{self.original_func}#{self.batch_arg_name}'

    def __get_pydantic_core_schema__(
        self,
        source_type: Any,
        handler: GetCoreSchemaHandler,
    ) -> core_schema.CoreSchema:
        return core_schema.with_info_plain_validator_function(
            self._validate,
            json_schema_input_schema=handler(source_type),
        )

    def _validate(self, value: Any, info: ValidationInfo) -> Any:
        if _is_the_same_type(value, self.return_type):
            return value

        positional: tuple[Any, ...] = (value, info) if self.has_info_arg else (value,)
        keyword: dict[str, Any] = {}
        if self.batch_arg_name is not None:
            if info.context is None:
                raise BatchContextMissingError(
                    f'Transformer {self.original_func.__name__!r} declares a BatchArg but no '
                    'validation context was provided. Call '
                    '`populate_context_with_batch(Model, rows)` (or `executor.render(Model, rows)`) '
                    'and pass the result as `context=` to `Model.model_validate`.',
                )
            ids = info.context[self.batch_key]
            keyword[self.batch_arg_name] = BatchArg(ids=frozenset(ids))

        return self.call(*positional, **keyword)

    def __repr__(self) -> str:
        func_name = getattr(self.original_func, '__name__', str(self.original_func))
        return f'TransformerFieldInfo({func_name}, batch_arg_name={self.batch_arg_name})'


def _is_the_same_type(value: Any, return_type: type) -> bool:
    if value.__class__ is return_type:
        return True

    origin_return_type = get_origin(return_type)
    args = get_args(return_type)
    if origin_return_type is Union or origin_return_type is builtin_types.UnionType:
        return any(_is_the_same_type(value, arg) for arg in args)
    if origin_return_type not in {list, set}:
        return False

    if not isinstance(value, origin_return_type):
        return False

    if len(value) == 0:
        return True

    item = next(iter(value))
    if not args:
        return True

    return _is_the_same_type(item, args[0])
