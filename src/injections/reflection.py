from collections.abc import Callable
from inspect import isclass
from typing import Annotated
from typing import Any
from typing import get_args
from typing import get_origin

from fastapi.params import Depends as FastDepends

from src.injections.dependant import cached_signature


def find_arg_info(func: Callable, target_type: type) -> tuple[str | None, type | None]:
    func_signature = cached_signature(func)
    for param_key, param_val in func_signature.parameters.items():
        param_annotation = param_val.annotation
        param_cls = param_annotation
        if get_origin(param_annotation) is Annotated:
            param_cls = param_annotation.__origin__
        if isclass(param_cls) and issubclass(param_cls, target_type):
            return param_key, param_annotation

    return None, None


def get_dependency_callable(annotation: type | Callable[..., Any]) -> Callable[..., Any]:
    arg_origin_type = get_origin(annotation)
    if arg_origin_type is not Annotated:
        raise ValueError('Expect to get Annotated object')

    arg_types_of_annotated = get_args(annotation)
    fastapi_depends_annotation = next(arg for arg in arg_types_of_annotated[1:] if isinstance(arg, FastDepends))
    assert fastapi_depends_annotation, f'Cannot find `Annotated[type, Depends(call)]` arguments for {annotation}'
    assert fastapi_depends_annotation.dependency, 'Depends must have callable arg'
    return fastapi_depends_annotation.dependency
