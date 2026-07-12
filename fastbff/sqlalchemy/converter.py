"""SQLAlchemy → fastbff row converter."""

from typing import Any
from typing import cast

from sqlalchemy import Select
from sqlalchemy.orm import Session


class SqlalchemyConverter:
    """Execute a SQLAlchemy ``Select`` and project rows into shape for the render pipeline.

    Replaces the ``[{'field': row.column, ...} for row in scalars]`` boilerplate
    inside ``@queries`` handlers. Column labels in the ``Select`` must match
    field names on the target Pydantic model — fastbff's render pipeline takes
    the rows from here and validates them through ``Query[T].T`` at the dispatch
    boundary (resolving any ``Resolve`` fields along the way), so the caller of
    ``query_executor.fetch(...)`` receives the declared model type.

    Per-request: bind via FastAPI ``Depends`` against your session factory::

        def make_converter(session: DBSession) -> SqlalchemyConverter:
            return SqlalchemyConverter(session)

        ConverterDep = Annotated[SqlalchemyConverter, Depends(make_converter)]

        @app.queries(FetchTeams)
        def fetch_teams(converter: ConverterDep) -> list[TeamDTO]:
            statement = select(TeamRow.id, TeamRow.owner_id.label('owner'))
            return converter.execute_all(statement, list[TeamDTO])

    The declared return type (``list[TeamDTO]``) describes what the handler's
    caller sees *after* fastbff's render pipeline; the converter returns rows
    under the hood and the framework validates them.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def execute_all[T](self, statement: Select[Any], return_type: type[T]) -> T:
        """Run *statement* and return rows shaped to ``return_type``.

        Pass the full output type — typically ``list[ModelT]``. The runtime
        result is a ``list[dict]``; the cast is honest because the framework
        validates it through ``Query[T].T`` before the value reaches any
        external caller.
        """
        rows = self.session.execute(statement).mappings().all()
        return cast(T, [dict(row) for row in rows])

    def execute_one[T](self, statement: Select[Any], return_type: type[T]) -> T | None:
        """Run *statement* and return the first row (or ``None``) shaped to ``return_type``.

        Use for ``Query[ModelT]`` (single-model) handlers.
        """
        row = self.session.execute(statement).mappings().first()
        return cast(T | None, dict(row) if row is not None else None)
