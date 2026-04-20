"""Sample FastBFF application wired through FastAPI + SQLAlchemy.

The full domain — SQLAlchemy models, Pydantic schemas, query / transformer
registrations, and a FastAPI route — packaged as a :func:`build_app`
factory so each test binds its own ``session_factory`` against an
isolated database.
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
from fastbff import build_transform_annotated
from fastbff import validate_batch


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


class User(BaseModel):
    id: int
    name: str


class FetchUsers(Query[dict[int, User]]):
    ids: frozenset[int]


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

app = FastBFF()
fastapi_app = FastAPI()


@app.queries
def fetch_users(args: FetchUsers, session: DBSession) -> dict[int, User]:
    rows = session.execute(select(UserRow).where(UserRow.id.in_(args.ids))).scalars().all()
    return {row.id: User(id=row.id, name=row.name) for row in rows}


@app.transformer
def transform_owner(
    owner_id: int,
    batch: BatchArg[int],
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> User | None:
    return query_executor.fetch(FetchUsers(ids=batch.ids)).get(owner_id)


OwnerTransformerAnnotated = build_transform_annotated(transform_owner)


class TeamDTO(BaseModel):
    id: int
    owner: OwnerTransformerAnnotated


class FetchTeams(Query[list[TeamDTO]]):
    pass


@app.queries(FetchTeams)
def fetch_teams(
    session: DBSession,
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> list[TeamDTO]:
    team_rows = session.execute(select(TeamRow)).scalars().all()
    rows = [{'id': row.id, 'owner': row.owner_id} for row in team_rows]
    return validate_batch(TeamDTO, rows, query_executor=query_executor)


@fastapi_app.get('/teams')
def list_teams(
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> list[TeamDTO]:
    return query_executor.fetch(FetchTeams())


app.mount(fastapi_app)
