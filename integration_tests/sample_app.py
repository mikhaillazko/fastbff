"""Sample FastBFF app wired for the FastAPI + SQLAlchemy integration tests.

Assembles the full stack — SQLAlchemy ORM models, Pydantic DTOs, query
and transformer registrations on a :class:`QueryRouter`, and a single
FastAPI route — as module-level singletons. Tests import
:data:`fastapi_app` and drive it through ``TestClient``.
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

from fastbff import BatchArg
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor
from fastbff import QueryRouter
from fastbff import build_transform_annotated
from fastbff import validate_batch

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


# --- DTOs ---------------------------------------------------------------------


class UserDTO(BaseModel):
    id: int
    name: str


# --- Transformers -------------------------------------------------------------
# Declared ahead of the query it fans into so the file reads top-down. The
# reference to ``FetchUsers`` inside the body is resolved at call time, not at
# decoration time, so the forward use is safe.


query_router = QueryRouter()


@query_router.transformer
def transform_owner_id_to_user(
    owner_id: int,
    batch: BatchArg[int],
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> UserDTO | None:
    return query_executor.fetch(FetchUsers(ids=batch.ids)).get(owner_id)


UserTransformerAnnotated = build_transform_annotated(transform_owner_id_to_user)


# --- Queries ------------------------------------------------------------------


class FetchUsers(Query[dict[int, UserDTO]]):
    ids: frozenset[int]


@query_router.queries
def fetch_users(args: FetchUsers, session: DBSession) -> dict[int, UserDTO]:
    rows = session.execute(select(UserRow).where(UserRow.id.in_(args.ids))).scalars().all()
    return {row.id: UserDTO(id=row.id, name=row.name) for row in rows}


class TeamDTO(BaseModel):
    id: int
    owner: UserTransformerAnnotated


class FetchTeams(Query[list[TeamDTO]]):
    pass


@query_router.queries(FetchTeams)
def fetch_teams(
    session: DBSession,
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> list[TeamDTO]:
    team_rows = session.execute(select(TeamRow)).scalars().all()
    rows = [{'id': row.id, 'owner': row.owner_id} for row in team_rows]
    return validate_batch(TeamDTO, rows, query_executor=query_executor)


# --- HTTP + mount -------------------------------------------------------------
# ``FastBFF.mount`` installs the synthesised ``QueryExecutor`` factory into
# ``fastapi_app.dependency_overrides`` so routes pull a per-request executor
# through plain ``Depends(QueryExecutor)``.


fastapi_app = FastAPI()


@fastapi_app.get('/teams')
def list_teams(
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> list[TeamDTO]:
    return query_executor.fetch(FetchTeams())


fastbff_app = FastBFF()
fastbff_app.include_router(query_router)
fastbff_app.mount(fastapi_app)
