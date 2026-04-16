import types
from collections.abc import Callable
from functools import lru_cache
from typing import Any
from typing import Union
from typing import get_args
from typing import get_origin
from typing import get_type_hints

from pydantic_core.core_schema import ValidationInfo

from src.injections.registry import IInjectorRegistry

from .types import _TRANSFORMER_ATTR
from .types import BatchArg
from .types import TransformerAnnotation


class TransformerRegistry:
    def __init__(self, injector: IInjectorRegistry) -> None:
        self._injector = injector
        self._transformer_map: dict[Callable, TransformerAnnotation] = {}

    def __call__(self, func: Callable) -> Callable:
        return self._register(func)

    def _register(self, func: Callable) -> Callable:
        hints = get_type_hints(func)
        return_type = hints.get('return')
        if return_type is None:
            raise TypeError(f'Transformer {func.__name__!r} must have a return type annotation')
        wrapped_transformer = self._injector.inject(func)
        annotation = TransformerAnnotation(call=wrapped_transformer, return_type=return_type)

        # follow pydantic validator signature -> any_plain_validator_func(val: Any, info: ValidationInfo)
        def plain_validator_decorator(val: Any, info: ValidationInfo) -> Any:
            if _is_the_same_type(val, annotation.return_type):
                return val

            args = (val, info) if annotation.has_info_arg else (val,)
            kwargs = {}
            if annotation.batch_arg_name is not None:
                if info.context is None:
                    raise RuntimeError(
                        f'Transformer {annotation!r} uses BatchArg but no context was provided. '
                        'Call populate_context_with_batch before validation.',
                    )
                ids = info.context[annotation.batch_key]
                kwargs[annotation.batch_arg_name] = BatchArg(ids=ids)

            return wrapped_transformer(*args, **kwargs)

        self._transformer_map[plain_validator_decorator] = annotation
        setattr(plain_validator_decorator, _TRANSFORMER_ATTR, annotation)
        return plain_validator_decorator


@lru_cache
def get_transformer_registry(injector_registry: IInjectorRegistry) -> TransformerRegistry:
    return TransformerRegistry(injector_registry)


def _is_the_same_type(val: Any, return_type: type) -> bool:
    if val.__class__ == return_type:
        return True

    origin_return_type = get_origin(return_type)
    args = get_args(return_type)
    if origin_return_type == Union or origin_return_type == types.UnionType:
        return any(_is_the_same_type(val, arg) for arg in args)
    if origin_return_type not in {list, set}:
        return False

    if not isinstance(val, origin_return_type):
        return False

    if len(val) == 0:
        return True

    item = next(iter(val))
    args = get_args(return_type)

    if not args:
        return True

    return _is_the_same_type(item, args[0])
