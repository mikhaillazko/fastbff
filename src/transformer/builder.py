from collections.abc import Callable
from typing import Annotated

from pydantic import PlainSerializer
from pydantic import PlainValidator

from .types import _TRANSFORMER_ATTR
from .types import TransformerAnnotation


def build_transform_annotated[T](func: Callable[..., T]) -> T:
    """Wrap a ``@transformer``-decorated callable into an ``Annotated`` type.

    The resulting type can be used directly as a Pydantic field annotation;
    during validation the wrapped callable is invoked with dependencies
    injected (and a ``BatchArg`` populated from the validation context if
    declared).
    """
    transformer_annotation = getattr(func, _TRANSFORMER_ATTR, None)
    assert isinstance(transformer_annotation, TransformerAnnotation)
    return_type = transformer_annotation.return_type
    return Annotated[  # type: ignore[return-value]
        return_type,  # type: ignore[valid-type]
        PlainValidator(func, json_schema_input_type=return_type),
        PlainSerializer(lambda v: v, return_type=return_type),
        transformer_annotation,
    ]
