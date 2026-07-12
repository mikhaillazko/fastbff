"""Public exception hierarchy for ``fastbff``.

All errors raised by the library subclass :class:`FastBFFError` so callers
can catch them with a single ``except`` clause. Sub-exceptions are typed by
concern (registration, caching, dependency resolution) so that targeted
handling is also possible.
"""


class FastBFFError(Exception):
    """Base class for all errors raised by ``fastbff``."""


class RegistrationError(FastBFFError):
    """Raised when a ``@queries`` handler or a ``Resolve`` field cannot be registered."""


class QueryRegistrationError(RegistrationError):
    """Raised when a ``@queries`` handler is mis-declared.

    Examples: missing return type, return type does not match ``Query[T]``,
    multiple ``Query[T]`` parameters on a single handler, an ``EntityQuery``
    without a discoverable ids field.
    """


class ResolveRegistrationError(RegistrationError):
    """Raised when a ``Resolve(...)`` field is mis-declared.

    Examples: neither/both of ``query_type`` and ``resolver`` supplied, more
    than one ``Resolve`` on a field, or a ``Resolve(QueryType)`` whose target is
    not a registered :class:`EntityQuery`.
    """


class QueryNotRegisteredError(FastBFFError, KeyError):
    """Raised when ``QueryExecutor.fetch`` receives a query class with no registered handler.

    Subclasses :class:`KeyError` for backwards compatibility with the previous
    behaviour of :meth:`QueryRouter.get_annotation_by_query_type`.
    """


class CacheKeyError(FastBFFError, TypeError):
    """Raised when a ``Query`` field value cannot be turned into a cache key.

    fastbff caches query results keyed by their arguments, so every field on a
    ``Query`` must be hashable or a shape fastbff can normalise (containers,
    pydantic models, dataclasses). Subclasses :class:`TypeError` because the
    underlying failure is an unhashable value.
    """
