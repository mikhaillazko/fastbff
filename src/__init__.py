"""pydantic-bff — simple back-end for front-end using Pydantic.

Public surface grouped by concern:

Transformer
-----------
- :class:`BatchArg` — marker parameter type for batch-aware transformers.
- :func:`build_transform_annotated` — wrap a ``@transformer`` into an ``Annotated`` field type.
- :func:`populate_context_with_batch` — Phase 1 helper that collects batch ids into a validation context.
- :class:`TransformerRegistry` / :func:`get_transformer_registry` — obtain the ``@transformer`` decorator.
- :func:`bff_model` — opt-in decorator that enables batch introspection on a Pydantic model.

Query executor
--------------
- :class:`Query` — typed query object (``Query[T]``).
- :class:`QueryExecutor` — per-request dispatcher with call-level + entity-level caching.
- :class:`QueryExecutorMock` — test double for stubbing queries.
- :class:`QueriesRegistry` / :func:`get_queries_registry` — obtain the ``@query`` decorator.

Injections (DI)
---------------
- :class:`InjectorRegistry` / :func:`get_injector_registry` — DI container.
- :class:`DependenciesSetup`, :class:`DependencyProvider`, :class:`WebDepends`, :func:`dependency` —
  composition helpers built on top of FastAPI's ``Depends``.
"""

from .injections import DependenciesSetup
from .injections import DependencyProvider
from .injections import WebDepends
from .injections.dependency import dependency
from .injections.registry import IInjectorRegistry
from .injections.registry import InjectorRegistry
from .injections.registry import get_injector_registry
from .query_executor.mock import QueryExecutorMock
from .query_executor.query import Query
from .query_executor.query_executor import QueryExecutor
from .query_executor.registry import IQueriesRegistry
from .query_executor.registry import QueriesRegistry
from .query_executor.registry import get_queries_registry
from .transformer.batcher import populate_context_with_batch
from .transformer.builder import build_transform_annotated
from .transformer.decorators import bff_model
from .transformer.inspection import introspect_model_transformers
from .transformer.registry import TransformerRegistry
from .transformer.registry import get_transformer_registry
from .transformer.types import BatchArg
from .transformer.types import BatchInfo
from .transformer.types import TransformerAnnotation

__all__ = [
    # Transformer
    'BatchArg',
    'BatchInfo',
    'TransformerAnnotation',
    'TransformerRegistry',
    'bff_model',
    'build_transform_annotated',
    'get_transformer_registry',
    'introspect_model_transformers',
    'populate_context_with_batch',
    # Query executor
    'IQueriesRegistry',
    'QueriesRegistry',
    'Query',
    'QueryExecutor',
    'QueryExecutorMock',
    'get_queries_registry',
    # Injections
    'DependenciesSetup',
    'DependencyProvider',
    'IInjectorRegistry',
    'InjectorRegistry',
    'WebDepends',
    'dependency',
    'get_injector_registry',
]
