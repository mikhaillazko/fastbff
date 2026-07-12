"""fastbff — declarative back-end-for-front-end on top of Pydantic + FastAPI.

Public surface
--------------

App / Router
~~~~~~~~~~~~
- :class:`FastBFF` — composition root: bundles the queries registry and exposes
  FastAPI-native DI via :meth:`FastBFF.mount`.
- :class:`QueryRouter` — local registration scope, attached via
  :meth:`FastBFF.include_router`.

Queries
~~~~~~~
- :class:`Query` — typed query object (``Query[T]``), call-level caching.
- :class:`EntityQuery` — ``Query[dict[K, V]]`` with an ids field, opting into
  entity-level caching (overlapping id sets share cached entries).
- :class:`QueryExecutor` — per-request, **async-native** dispatcher. Async
  endpoints ``await query_executor.fetch(query)``.
- :class:`SyncQueryExecutor` — sync facade for sync endpoints;
  ``sync_query_executor.fetch(query)`` bridges onto the event loop.
- :class:`QueryExecutorMock` — test double for stubbing queries.

Composition
~~~~~~~~~~~
- :class:`Resolve` — field annotation declaring how a relation field is
  populated (``Resolve(SomeEntityQuery)`` or ``Resolve(resolver=fn)``). The
  executor runs a plan → concurrent-fetch → merge pipeline; ``model_validate``
  stays context-free.

Exceptions
~~~~~~~~~~
- :class:`FastBFFError` and its subclasses — see :mod:`fastbff.exceptions`.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

from .app import FastBFF
from .exceptions import CacheKeyError
from .exceptions import FastBFFError
from .exceptions import QueryNotRegisteredError
from .exceptions import QueryRegistrationError
from .exceptions import RegistrationError
from .exceptions import ResolveRegistrationError
from .query_executor.query import EntityQuery
from .query_executor.query import Query
from .query_executor.query_executor import QueryExecutor
from .query_executor.query_executor import SyncQueryExecutor
from .query_executor.query_executor_mock import QueryExecutorMock
from .resolve import Resolve
from .router import QueryRouter

try:
    __version__ = _version('fastbff')
except PackageNotFoundError:  # pragma: no cover - source checkout without an install
    __version__ = '0.0.0+unknown'

__all__ = [
    '__version__',
    # App / Router
    'FastBFF',
    'QueryRouter',
    # Queries
    'EntityQuery',
    'Query',
    'QueryExecutor',
    'QueryExecutorMock',
    'SyncQueryExecutor',
    # Composition
    'Resolve',
    # Exceptions
    'CacheKeyError',
    'FastBFFError',
    'QueryNotRegisteredError',
    'QueryRegistrationError',
    'RegistrationError',
    'ResolveRegistrationError',
]
