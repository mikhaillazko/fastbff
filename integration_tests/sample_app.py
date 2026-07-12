"""Sample FastBFF app wired for the FastAPI + SQLAlchemy integration tests.

Assembles the full stack — SQLAlchemy ORM models, Pydantic DTOs, query
registrations on a :class:`QueryRouter`, and FastAPI routes — as module-level
singletons. Tests import :data:`fastapi_app` and drive it through ``TestClient``.
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends
from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import Session
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fastbff import EntityQuery
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import QueryRouter
from fastbff import Resolve
from fastbff import SyncQueryExecutor
from fastbff.sqlalchemy import SqlalchemyConverter

# --- Persistence --------------------------------------------------------------
# ``StaticPool`` + ``check_same_thread=False`` keeps a single SQLite connection
# alive for the whole process so schema and seed data populated by the test
# fixture are visible to requests served through ``TestClient`` — a fresh
# connection per checkout would otherwise see an empty in-memory database.


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class TeamRow(Base):
    __tablename__ = 'teams'
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int]


db_engine = create_engine(
    'sqlite:///:memory:',
    future=True,
    connect_args={'check_same_thread': False},
    poolclass=StaticPool,
)

session_factory = sessionmaker(bind=db_engine, expire_on_commit=False)


def get_db_session() -> Iterator[Session]:
    with session_factory() as session:
        yield session


DBSession = Annotated[Session, Depends(get_db_session)]


def make_sqlalchemy_converter(session: DBSession) -> SqlalchemyConverter:
    return SqlalchemyConverter(session)


SqlalchemyConverterDep = Annotated[SqlalchemyConverter, Depends(make_sqlalchemy_converter)]


# --- DTOs ---------------------------------------------------------------------


class UserDTO(BaseModel):
    id: int
    name: str


# --- Queries ------------------------------------------------------------------
# ``FetchUsers`` is an EntityQuery: overlapping id sets share cached entries and
# only missing ids are fetched. ``TeamDTO.owner`` declares ``Resolve(FetchUsers)``
# so the raw ``owner_id`` on each row is resolved to a ``UserDTO`` by the render
# pipeline — a single bulk SELECT for the whole page.


query_router = QueryRouter()


class FetchUsers(EntityQuery[int, UserDTO]):
    ids: frozenset[int]


@query_router.queries
def fetch_users(query: FetchUsers, session: DBSession) -> dict[int, UserDTO]:
    rows = session.execute(select(UserRow).where(UserRow.id.in_(query.ids))).scalars().all()
    return {row.id: UserDTO(id=row.id, name=row.name) for row in rows}


class TeamDTO(BaseModel):
    id: int
    owner: Annotated[UserDTO | None, Resolve(FetchUsers)]


class FetchTeams(Query[list[TeamDTO]]):
    pass


@query_router.queries(FetchTeams)
def fetch_teams(sqlalchemy_converter: SqlalchemyConverterDep) -> list[TeamDTO]:
    statement = select(TeamRow.id, TeamRow.owner_id.label('owner'))
    return sqlalchemy_converter.execute_all(statement, list[TeamDTO])


# --- HTTP + mount -------------------------------------------------------------
# ``FastBFF.mount`` installs the synthesised ``QueryExecutor`` and
# ``SyncQueryExecutor`` factories into ``fastapi_app.dependency_overrides`` so
# routes pull a per-request executor through plain ``Depends(...)``.


fastapi_app = FastAPI()


@fastapi_app.get('/teams')
def list_teams(
    sync_query_executor: Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)],
) -> list[TeamDTO]:
    # Sync endpoint: Starlette runs it in a worker thread; SyncQueryExecutor
    # bridges the async executor onto the loop. One bulk SELECT for owners.
    return sync_query_executor.fetch(FetchTeams())


@fastapi_app.get('/teams-async')
async def list_teams_async(
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> list[TeamDTO]:
    # Async endpoint: the executor runs loop-native. Same payload and same
    # single-bulk-SELECT N+1 contract as the sync ``/teams`` route.
    return await query_executor.fetch(FetchTeams())


fastbff_app = FastBFF()
fastbff_app.include_router(query_router)
fastbff_app.mount(fastapi_app)
