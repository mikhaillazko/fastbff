from typing import Annotated

from fastapi import Depends


def dependency[T](service: type[T]) -> type[T]:
    """Shorthand for ``Annotated[Service, Depends(Service)]``.

    Decorate a class with ``@dependency`` to declare it as injectable with
    itself as the factory, so callers can annotate parameters as plain
    ``Service`` and still have FastAPI resolve them.
    """
    return Annotated[service, Depends(service)]  # type: ignore[return-value]
