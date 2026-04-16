from collections.abc import Callable
from functools import lru_cache
from typing import Annotated
from typing import Any
from typing import get_args
from typing import get_origin
from typing import get_type_hints

from fastbff.exceptions import TransformerRegistrationError
from fastbff.injections.registry import IInjectorRegistry

from .types import _FIELD_INFO_ATTR
from .types import TransformerFieldInfo


class TransformerRegistry:
    """Decorator factory: ``@transformer`` registers a function as a transformer.

    The decorator returns the original function unchanged, with a
    :class:`TransformerFieldInfo` attached at ``func._transformer_field_info``.
    Use :func:`build_transform_annotated` to build a Pydantic-ready
    ``Annotated[ReturnType, TransformerFieldInfo]`` alias; bind it to a
    PascalCase ``<Name>TransformerAnnotated`` name and use it directly as a
    field type::

        @transformer
        def transform_owner(
            owner_id: int,
            query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
        ) -> User: ...

        OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

        class TeamDTO(BaseModel):
            owner: OwnerTransformerAnnotated
    """

    def __init__(self, injector: IInjectorRegistry) -> None:
        self._injector = injector

    def __call__(self, func: Callable) -> Callable:
        """Register *func* as a transformer::

            @transformer
            def transform_owner(
                owner_id: int,
                batch: BatchArg[int],
                query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
            ) -> User | None: ...

        The transformer body is responsible for fetching its own data (e.g.
        ``query_executor.fetch(FetchUsers(ids=batch.ids))``). The query
        executor's cache dedups across rows, so one bulk call per page is
        issued on the first row and subsequent rows hit the cache.
        """
        return self._register(func)

    def _register[F: Callable](self, func: F) -> F:
        hints = get_type_hints(func)
        return_type = hints.get('return')
        if return_type is None:
            raise TransformerRegistrationError(
                f'Transformer {func.__name__!r} must have a return type annotation.',
            )
        wrapped_call = self._injector.inject(func)
        field_info = TransformerFieldInfo(
            original_func=func,
            wrapped_call=wrapped_call,
            return_type=return_type,
        )
        setattr(func, _FIELD_INFO_ATTR, field_info)
        return func


def build_transform_annotated(func: Callable) -> Any:
    """Return an ``Annotated[ReturnType, TransformerFieldInfo]`` alias for *func*.

    The result is a Pydantic-ready type alias usable directly as a field type
    on a Pydantic model. Bind it to a PascalCase
    ``<Name>TransformerAnnotated`` name to signal that it is a type alias::

        @transformer
        def transform_owner(owner_id: int) -> User | None: ...

        OwnerTransformerAnnotated = build_transform_annotated(transform_owner)

        class TeamDTO(BaseModel):
            owner: OwnerTransformerAnnotated

    The return type baked into the alias is exactly the function's declared
    return type — including ``Optional``, ``list[...]``, etc.

    Raises :class:`TransformerRegistrationError` if *func* was never registered
    via ``@transformer``.
    """
    field_info = getattr(func, _FIELD_INFO_ATTR, None)
    if not isinstance(field_info, TransformerFieldInfo):
        func_name = getattr(func, '__name__', repr(func))
        raise TransformerRegistrationError(
            f'{func_name!r} is not a registered @transformer — '
            'decorate it with @transformer before calling build_transform_annotated().',
        )
    return Annotated[field_info.return_type, field_info]


def transformer_callable(func_or_field: Any) -> Callable | None:
    """Return the DI-wrapped underlying callable for a ``@transformer`` function.

    Accepts either the registered function itself or an
    ``Annotated[T, build_transform_annotated(func)]`` alias::

        @transformer
        def transform_owner(owner_id: int) -> User: ...

        call = transformer_callable(transform_owner)
        assert call(owner_id=1) == User(id=1, name='...')
    """
    field_info = transformer_metadata(func_or_field)
    return field_info.call if field_info is not None else None


def transformer_metadata(func_or_field: Any) -> TransformerFieldInfo | None:
    """Return the :class:`TransformerFieldInfo` for a transformer or field annotation.

    Accepts either the original ``@transformer``-decorated function (the
    metadata is read off ``func._transformer_field_info``) or an
    ``Annotated[ReturnType, TransformerFieldInfo]`` alias.
    """
    direct = getattr(func_or_field, _FIELD_INFO_ATTR, None)
    if isinstance(direct, TransformerFieldInfo):
        return direct
    if isinstance(func_or_field, TransformerFieldInfo):
        return func_or_field
    if get_origin(func_or_field) is Annotated:
        for meta in get_args(func_or_field)[1:]:
            if isinstance(meta, TransformerFieldInfo):
                return meta
    return None


@lru_cache
def get_transformer_registry(injector_registry: IInjectorRegistry) -> TransformerRegistry:
    return TransformerRegistry(injector_registry)
