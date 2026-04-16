"""fastbff — declarative back-end-for-front-end on top of Pydantic + FastAPI.

Public surface
--------------

App / Router
~~~~~~~~~~~~
- :class:`FastBFF` — composition root: bundles the queries registry, transformer
  registry, DI container, and a :class:`QueryExecutor`.
- :class:`QueryRouter` — local registration scope, attached via
  :meth:`FastBFF.include_router`.

Composition
~~~~~~~~~~~
- :class:`BatchArg` — marker parameter type for batch-aware transformers.
- :func:`build_transform_annotated` — build the ``Annotated[...]`` metadata
  for a ``@transformer``-registered function.
- :class:`TransformerFieldInfo` — the metadata object placed inside
  ``Annotated[ReturnType, ...]`` (returned by ``build_transform_annotated``).
- :func:`populate_context_with_batch` — Phase 1 helper (for manual orchestration).

Queries
~~~~~~~
- :class:`Query` — typed query object (``Query[T]``).
- :class:`QueryExecutor` — per-request dispatcher; :meth:`render` does Plan/Fetch/Merge in one call.
- :class:`QueriesRegistry` — ``@queries`` decorator factory.
- :class:`QueryExecutorMock` — test double for stubbing queries.

Dependency injection
~~~~~~~~~~~~~~~~~~~~
- :class:`InjectorRegistry` — DI container (``inject``, ``entrypoint``, ``bind``).
- :class:`TransformerRegistry` — ``@transformer`` decorator factory.

Test helpers
~~~~~~~~~~~~
- :func:`transformer_callable` / :func:`transformer_metadata` — extract underlying
  function or metadata from a ``@transformer`` function or annotated alias.

Exceptions
~~~~~~~~~~
- :class:`FastBFFError` and its subclasses — see :mod:`fastbff.exceptions`.
"""

from .app import FastBFF
from .exceptions import BatchContextMissingError
from .exceptions import DependencyOverrideError
from .exceptions import DependencyResolutionError
from .exceptions import FastBFFError
from .exceptions import QueryNotRegisteredError
from .exceptions import QueryRegistrationError
from .exceptions import RegistrationError
from .exceptions import TransformerRegistrationError
from .injections.dependencies_setup import DependenciesSetup
from .injections.dependency_provider import DependencyProvider
from .injections.registry import InjectorRegistry
from .injections.registry import get_injector_registry
from .query_executor.mock import QueryExecutorMock
from .query_executor.query import Query
from .query_executor.query_executor import QueryExecutor
from .query_executor.registry import QueriesRegistry
from .query_executor.registry import get_queries_registry
from .router import QueryRouter
from .transformer.batcher import populate_context_with_batch
from .transformer.registry import TransformerRegistry
from .transformer.registry import build_transform_annotated
from .transformer.registry import get_transformer_registry
from .transformer.registry import transformer_callable
from .transformer.registry import transformer_metadata
from .transformer.types import BatchArg
from .transformer.types import TransformerFieldInfo

__all__ = [
    # App / Router
    'FastBFF',
    'QueryRouter',
    # Composition
    'BatchArg',
    'populate_context_with_batch',
    # Queries
    'QueriesRegistry',
    'Query',
    'QueryExecutor',
    'QueryExecutorMock',
    'get_queries_registry',
    # DI
    'DependenciesSetup',
    'DependencyProvider',
    'InjectorRegistry',
    'TransformerFieldInfo',
    'TransformerRegistry',
    'build_transform_annotated',
    'get_injector_registry',
    'get_transformer_registry',
    # Test helpers
    'transformer_callable',
    'transformer_metadata',
    # Exceptions
    'BatchContextMissingError',
    'DependencyOverrideError',
    'DependencyResolutionError',
    'FastBFFError',
    'QueryNotRegisteredError',
    'QueryRegistrationError',
    'RegistrationError',
    'TransformerRegistrationError',
]
