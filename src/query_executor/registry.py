from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from src.injections.registry import IInjectorRegistry

from .query_annotation import QueryAnnotation
from .query_annotation import extract_query_return_type


class QueriesRegistry:
    """Process-wide registry of ``@query`` handlers keyed by query class."""

    def __init__(self, injector: IInjectorRegistry) -> None:
        self._injector = injector
        self._query_annotations: dict[type, QueryAnnotation] = {}

    def __call__[F: Callable](self, func: F) -> F:
        wrapped_func = self._injector.inject(func)
        annotation = QueryAnnotation(call=wrapped_func, original_func=func)
        assert annotation.return_type is not None, 'Query must have return type and return values'
        if annotation.query_type is not None:
            expected_return = extract_query_return_type(annotation.query_type)
            if expected_return is not None:
                assert annotation.return_type == expected_return, (
                    f'@query {func.__name__}: return type {annotation.return_type} '
                    f'does not match {annotation.query_type.__name__}[{expected_return}]'
                )
            self._query_annotations[annotation.query_type] = annotation
        return func

    def get_annotation_by_query_type(self, query_type: type) -> QueryAnnotation:
        annotation = self._query_annotations.get(query_type)
        if annotation is not None:
            return annotation
        raise KeyError(f'No @query registered for query object {query_type}')


@lru_cache
def get_queries_registry(injector_registry: IInjectorRegistry) -> QueriesRegistry:
    return QueriesRegistry(injector_registry)


IQueriesRegistry = Annotated[QueriesRegistry, Depends(get_queries_registry)]
