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
from .types import TransformerFieldInfo


def introspect_model_transformers(cls: type[PydanticBaseModel]) -> None:
    """Scan *cls*'s annotations for transformer fields with a ``BatchArg`` and
    cache the batching metadata on ``cls.__batches__``.

    Invoked lazily on first use (via :func:`get_model_batches`). No-ops when
    the model has no batchable transformer fields.
    """
    batches = []
    annotations = get_annotations(cls)
    for field_name, field_type in annotations.items():
        field_info = _find_transformer_field_info(field_type)
        if field_info and field_info.batch_key:
            batches.append(
                BatchInfo(
                    field_name=field_name,
                    key=field_info.batch_key,
                    batch_fetch_type=field_info.batch_fetch_type,
                ),
            )

    setattr(cls, _BATCHES_ATTR, batches)


def _find_transformer_field_info(field_type: type) -> TransformerFieldInfo | None:
    metadata = _find_all_nested_annotations(field_type)
    field_infos = [item for item in metadata if isinstance(item, TransformerFieldInfo)]
    if not field_infos:
        return None
    assert len(field_infos) == 1, field_infos
    return field_infos[0]


def _find_all_nested_annotations(_type: type) -> list[Any]:
    annotations: list[Any] = []
    origin = get_origin(_type)

    if origin is Annotated:
        # For Annotated[T, a1, a2, ...], get_args returns (T, a1, a2, ...)
        args = get_args(_type)
        base_type = args[0]
        for meta in args[1:]:
            # When user-supplied metadata is itself an Annotated alias, recurse
            # into it explicitly — Python does not flatten the outer Annotated
            # into the inner one if the base types do not match by identity.
            if get_origin(meta) is Annotated:
                annotations.extend(_find_all_nested_annotations(meta))
            else:
                annotations.append(meta)
        annotations.extend(_find_all_nested_annotations(base_type))
    elif origin in (Union, Optional) or get_args(_type):
        for arg in get_args(_type):
            annotations.extend(_find_all_nested_annotations(arg))

    return annotations
