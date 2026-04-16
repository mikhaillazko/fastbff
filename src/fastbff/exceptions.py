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

    Example: missing return type annotation.
    """


class QueryNotRegisteredError(FastBFFError, KeyError):
    """Raised when ``QueryExecutor.fetch`` receives a query class with no registered handler.

    Subclasses :class:`KeyError` for backwards compatibility with the previous
    behaviour of :meth:`QueriesRegistry.get_annotation_by_query_type`.
    """


class BatchContextMissingError(FastBFFError, RuntimeError):
    """Raised when a transformer with a ``BatchArg`` is invoked without a batching context.

    Almost always means ``populate_context_with_batch`` (or ``executor.render``)
    was not called, or ``model_validate`` was invoked without ``context=`` kwarg.
    """


class ModelNotPreparedError(FastBFFError, RuntimeError):
    """Raised when a model is asked for batching metadata before introspection."""


class DependencyResolutionError(FastBFFError):
    """Raised when one or more ``Depends(...)`` parameters fail to resolve."""

    def __init__(self, errors: list[object]) -> None:
        super().__init__(f'Failed to resolve dependencies: {errors!r}')
        self.errors = errors


class DependencyOverrideError(FastBFFError, KeyError):
    """Raised when ``DependenciesSetup.override`` targets an unregistered interface."""
