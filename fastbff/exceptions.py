"""Public exception hierarchy for ``fastbff``.

All errors raised by the library subclass :class:`FastBFFError` so callers
can catch them with a single ``except`` clause. Sub-exceptions are typed by
concern (registration, batching, dependency resolution) so that targeted
handling is also possible.
"""


class FastBFFError(Exception):
    """Base class for all errors raised by ``fastbff``."""


class RegistrationError(FastBFFError):
    """Raised when a ``@query`` or ``@transformer`` cannot be registered."""


class QueryRegistrationError(RegistrationError):
    """Raised when a ``@query`` handler is mis-declared.

    Examples: missing return type, return type does not match ``Query[T]``,
    multiple ``Query[T]`` parameters on a single handler.
    """


class TransformerRegistrationError(RegistrationError):
    """Raised when a ``@transformer`` callable is mis-declared.

    Example: missing return type annotation, multiple transformer annotations
    on a single model field.
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


class BatchContextMissingError(FastBFFError, RuntimeError):
    """Raised when a transformer with a ``BatchArg`` is invoked without a batching context.

    Almost always means a row was validated via plain ``Model.model_validate``
    instead of going through a fastbff dispatch boundary.
    ``QueryExecutor.fetch`` builds the batch context automatically when the
    declared return type is a model with transformer fields — invoke the
    handler instead of validating models by hand.
    """
