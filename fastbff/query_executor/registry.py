from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from fastbff.exceptions import QueryNotRegisteredError
from fastbff.exceptions import QueryRegistrationError
from fastbff.injections.registry import IInjectorRegistry

from .query_annotation import QueryAnnotation
from .query_annotation import extract_query_return_type


class QueriesRegistry:
    """Process-wide registry of ``@query`` handlers.

    Two equivalent registration shapes are supported:

    * ``Query[T]`` form — a single ``Query[T]`` parameter on the function.
      Dispatch via ``executor.fetch(query_obj)``.
    * Function-signature form — plain typed parameters, no ``Query[T]`` subclass
      required. Dispatch via ``executor.call(handler, **kwargs)``.
    """

    def __init__(self, injector: IInjectorRegistry) -> None:
        self._injector = injector
        self._query_annotations: dict[type, QueryAnnotation] = {}
        self._func_annotations: dict[Callable, QueryAnnotation] = {}

    def __call__[F: Callable](self, func: F) -> F:
        wrapped_func = self._injector.inject(func)
        annotation = QueryAnnotation(call=wrapped_func, original_func=func)
        if annotation.return_type is None:
            raise QueryRegistrationError(
                f'@query {func.__name__!r}: handler must declare a return type annotation.',
            )
        if annotation.query_type is not None:
            expected_return = extract_query_return_type(annotation.query_type)
            if expected_return is not None and annotation.return_type != expected_return:
                raise QueryRegistrationError(
                    f'@query {func.__name__}: return type {annotation.return_type} '
                    f'does not match {annotation.query_type.__name__}[{expected_return}]',
                )
            self._query_annotations[annotation.query_type] = annotation
        self._func_annotations[func] = annotation
        return func

    def get_annotation_by_query_type(self, query_type: type) -> QueryAnnotation:
        annotation = self._query_annotations.get(query_type)
        if annotation is not None:
            return annotation
        raise QueryNotRegisteredError(f'No @query registered for query object {query_type}')

    def get_annotation_by_func(self, func: Callable) -> QueryAnnotation:
        annotation = self._func_annotations.get(func)
        if annotation is not None:
            return annotation
        raise QueryNotRegisteredError(f'No @query registered for callable {func!r}')


@lru_cache
def get_queries_registry(injector_registry: IInjectorRegistry) -> QueriesRegistry:
    return QueriesRegistry(injector_registry)


IQueriesRegistry = Annotated[QueriesRegistry, Depends(get_queries_registry)]
