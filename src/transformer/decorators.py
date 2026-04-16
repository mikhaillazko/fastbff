from pydantic import BaseModel as PydanticBaseModel

from .inspection import introspect_model_transformers


def bff_model[T: type[PydanticBaseModel]](cls: T) -> T:
    """Class decorator that enables batch-transformer introspection on *cls*.

    Inspects the model's annotations for fields whose metadata includes a
    :class:`TransformerAnnotation` with a ``BatchArg`` parameter, and caches
    the batching metadata on the class so that
    :func:`populate_context_with_batch` can populate a validation context
    for them. Silently no-ops for models without any batchable fields.

    Usage::

        from pydantic import BaseModel
        from src import bff_model

        @bff_model
        class TeamDTO(BaseModel):
            id: int
            users: Annotated[list[UserDTO], UserBatchTransformer]
    """
    assert issubclass(cls, PydanticBaseModel), f'@bff_model expects a Pydantic BaseModel subclass, got {cls!r}'
    introspect_model_transformers(cls)
    return cls
