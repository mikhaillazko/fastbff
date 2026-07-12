"""fastbff — declarative back-end-for-front-end on top of Pydantic + FastAPI.

Public surface
--------------

App / Router
~~~~~~~~~~~~
- :class:`FastBFF` — composition root: bundles the queries registry, transformer
  registry, and exposes FastAPI-native DI via :meth:`FastBFF.mount`.
- :class:`QueryRouter` — local registration scope, attached via
  :meth:`FastBFF.include_router`.

Composition
~~~~~~~~~~~
- :class:`BatchArg` — marker parameter type for batch-aware transformers.
- :func:`build_transform_annotated` — build the ``Annotated[...]`` metadata
  for a ``@transformer``-registered function.
- :class:`TransformerAnnotation` — the metadata object placed inside
  ``Annotated[ReturnType, ...]`` (returned by ``build_transform_annotated``).

Queries
~~~~~~~
- :class:`Query` — typed query object (``Query[T]``).
- :class:`QueryExecutor` — per-request dispatcher with call-level and
  entity-level caching. Auto-wraps handler results through ``validate_batch``
  when the handler's declared return type is a :class:`pydantic.BaseModel`
  (or ``list`` thereof) with transformer fields, so end users never call
  ``validate_batch`` directly.
- :class:`QueryExecutorMock` — test double for stubbing queries.

Test helpers
~~~~~~~~~~~~
- :func:`transformer_metadata` — extract the :class:`TransformerAnnotation`
  attached to a ``@transformer`` function or an annotated field alias.

Exceptions
~~~~~~~~~~
- :class:`FastBFFError` and its subclasses — see :mod:`fastbff.exceptions`.
"""

from .app import FastBFF
from .exceptions import AsyncDispatchError
from .exceptions import BatchContextMissingError
from .exceptions import CacheKeyError
from .exceptions import FastBFFError
from .exceptions import QueryNotRegisteredError
from .exceptions import QueryRegistrationError
from .exceptions import RegistrationError
from .exceptions import TransformerRegistrationError
from .query_executor.query import Query
from .query_executor.query_executor import QueryExecutor
from .query_executor.query_executor_mock import QueryExecutorMock
from .router import QueryRouter
from .transformer.registry import build_transform_annotated
from .transformer.registry import transformer_metadata
from .transformer.types import BatchArg
from .transformer.types import TransformerAnnotation

__all__ = [
    # App / Router
    'FastBFF',
    'QueryRouter',
    # Composition
    'BatchArg',
    # Queries
    'Query',
    'QueryExecutor',
    'QueryExecutorMock',
    # Transformer
    'TransformerAnnotation',
    'build_transform_annotated',
    # Test helpers
    'transformer_metadata',
    # Exceptions
    'AsyncDispatchError',
    'BatchContextMissingError',
    'CacheKeyError',
    'FastBFFError',
    'QueryNotRegisteredError',
    'QueryRegistrationError',
    'RegistrationError',
    'TransformerRegistrationError',
]
