from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from functools import cache
from functools import cached_property
from inspect import Signature
from inspect import isgeneratorfunction
from inspect import signature
from typing import Annotated
from typing import Any
from typing import cast
from typing import get_args
from typing import get_origin

from fastapi.params import Depends as ParamDepends

from .web_depends import WebDepends


@cache
def cached_signature(func: Callable) -> Signature:
    return signature(func)


@dataclass(kw_only=True, frozen=True)
class Dependant:
    call: Callable
    name: str = ''
    use_cache: bool = True
    dependencies: list[Dependant] = field(default_factory=list)

    def __post_init__(self) -> None:
        dependencies = _introspect_dependencies(self)
        self.dependencies.extend(dependencies)

    @property
    def cache_key(self) -> Callable:
        return self.call

    @cached_property
    def is_gen_callable(self) -> bool:
        if isgeneratorfunction(self.call):
            return True
        dunder_call = getattr(self.call, '__call__', None)  # noqa: B004
        return isgeneratorfunction(dunder_call)

    def __repr__(self) -> str:
        return f'Dependant(name={self.name}, call={self.call!r})'


def _introspect_dependencies(dependant: Dependant) -> list[Dependant]:
    dependencies = []
    func_signature = cached_signature(dependant.call)
    for arg_param in func_signature.parameters.values():
        arg_origin_type = get_origin(arg_param.annotation)
        if arg_origin_type is not Annotated:
            continue

        arg_types_of_annotated = get_args(arg_param.annotation)
        annotated_metadata = arg_types_of_annotated[1:]

        if any(isinstance(arg, WebDepends) for arg in annotated_metadata):
            continue

        fastapi_annotations = [arg for arg in annotated_metadata if isinstance(arg, ParamDepends)]
        assert len(fastapi_annotations) <= 1, f'Cannot specify multiple `Annotated` arguments for {arg_param.name!r}'
        if fastapi_annotations:
            depends_obj = fastapi_annotations[0]
            call = cast(Callable[..., Any], depends_obj.dependency)
            sub_dependant = Dependant(
                call=call,
                name=arg_param.name,
                use_cache=depends_obj.use_cache,
            )
            dependencies.append(sub_dependant)

    return dependencies
