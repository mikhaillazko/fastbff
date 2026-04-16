from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel as PydanticBaseModel

from .inspection import introspect_model_transformers
from .types import _BATCHES_ATTR
from .types import BatchInfo


def populate_context_with_batch(
    return_model: type[PydanticBaseModel],
    dict_objects: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Phase 1 — scan *dict_objects* for every batchable field on *return_model*
    and return a Pydantic validation context populated with the collected IDs.

    If *return_model* has not been introspected yet, introspection runs lazily
    here, so models work out of the box.

    Pass the result as ``context=...`` when calling ``Model.model_validate`` so
    that transformers using ``BatchArg`` can read the full ID set from the
    validation context.
    """
    batches_info = get_model_batches(return_model)
    context_cache: dict[str, Any] = {}
    for batch_info in batches_info:
        batch_values: set[Any] = set()
        for dict_object in dict_objects:
            field_value = dict_object[batch_info.field_name]
            if isinstance(field_value, Iterable):
                for value in field_value:
                    if value is None:
                        continue
                    batch_values.add(value)
            elif field_value is not None:
                batch_values.add(field_value)
        context_cache.setdefault(batch_info.key, set()).update(batch_values)
    return context_cache


def get_model_batches(return_model: type[PydanticBaseModel]) -> list[BatchInfo]:
    """Return the cached :class:`BatchInfo` list for *return_model*, introspecting on demand."""
    cached = getattr(return_model, _BATCHES_ATTR, None)
    if cached is not None:
        return cached
    introspect_model_transformers(return_model)
    return getattr(return_model, _BATCHES_ATTR, [])
