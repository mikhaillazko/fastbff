from inspect import get_annotations
from typing import Annotated
from typing import Any
from typing import Optional
from typing import Union
from typing import get_args
from typing import get_origin

from pydantic import BaseModel as PydanticBaseModel

from .types import _BATCHES_ATTR
from .types import BatchInfo
from .types import TransformerAnnotation


def introspect_model_transformers(cls: type[PydanticBaseModel]) -> None:
    """Scan *cls*'s annotations for transformer fields with a ``BatchArg`` and
    cache the batching metadata on ``cls.__batches__``.

    Invoked once per model class, typically via the :func:`bff_model`
    decorator. No-ops when the model has no batchable transformer fields.
    """
    batches = []
    annotations = get_annotations(cls)
    for field_name, field_type in annotations.items():
        transformer_annotation = _find_transformer_annotation(field_type)
        if transformer_annotation and transformer_annotation.batch_key:
            batch_info = BatchInfo(
                field_name=field_name,
                key=transformer_annotation.batch_key,
                batch_fetch_type=transformer_annotation.batch_fetch_type,
            )
            batches.append(batch_info)

    if batches:
        setattr(cls, _BATCHES_ATTR, batches)


def _find_transformer_annotation(field_type: type) -> TransformerAnnotation | None:
    metadata = _find_all_nested_annotations(field_type)
    transformer_annotations = [item for item in metadata if isinstance(item, TransformerAnnotation)]
    if not transformer_annotations:
        return None
    assert len(transformer_annotations) == 1, transformer_annotations
    return transformer_annotations[0]


def _find_all_nested_annotations(_type: type) -> list[Any]:
    annotations: list[Any] = []
    origin = get_origin(_type)

    if origin is Annotated:
        # For Annotated[T, a1, a2, ...], get_args returns (T, a1, a2, ...)
        args = get_args(_type)
        base_type = args[0]
        for meta in args[1:]:
            # When the user-supplied metadata is itself an Annotated alias
            # (as produced by build_transform_annotated), Python does not
            # flatten the outer Annotated into the inner one when the base
            # types do not match by identity, so recurse into it explicitly.
            if get_origin(meta) is Annotated:
                annotations.extend(_find_all_nested_annotations(meta))
            else:
                annotations.append(meta)
        # Recurse on the base type
        annotations.extend(_find_all_nested_annotations(base_type))
    elif origin in (Union, Optional):
        # For Union or Optional, recurse on all argument types
        for arg in get_args(_type):
            annotations.extend(_find_all_nested_annotations(arg))
    elif get_args(_type):
        # For other generic types (e.g., list), recurse on parameters
        for arg in get_args(_type):
            annotations.extend(_find_all_nested_annotations(arg))
    # Simple types (e.g., int, NoneType) have no annotations

    return annotations
