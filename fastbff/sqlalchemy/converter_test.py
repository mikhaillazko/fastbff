"""Unit tests for :class:`SqlalchemyConverter`.

Spins up an in-memory SQLite to exercise the SQL → row mapping path
without involving the rest of fastbff. The integration test under
``integration_tests/`` covers the converter wired through a real
``@queries`` handler + ``Resolve`` field composition.
"""

from typing import Any

from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy import select
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import Session
from sqlalchemy.orm import mapped_column

from fastbff.sqlalchemy import SqlalchemyConverter


class _Base(DeclarativeBase):
    pass


class _ItemRow(_Base):
    __tablename__ = 'items'
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str]


class _ItemDTO(BaseModel):
    id: int
    label: str


def _seeded_session() -> Session:
    engine = create_engine('sqlite:///:memory:', future=True)
    _Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    session.add_all([_ItemRow(id=1, label='a'), _ItemRow(id=2, label='b')])
    session.commit()
    return session


def test_execute_all_returns_rows_keyed_by_field_name() -> None:
    converter = SqlalchemyConverter(_seeded_session())

    rows: Any = converter.execute_all(select(_ItemRow.id, _ItemRow.label), list[_ItemDTO])

    assert rows == [{'id': 1, 'label': 'a'}, {'id': 2, 'label': 'b'}]


def test_execute_one_returns_first_row_or_none() -> None:
    converter = SqlalchemyConverter(_seeded_session())

    first: Any = converter.execute_one(select(_ItemRow.id, _ItemRow.label).where(_ItemRow.id == 1), _ItemDTO)
    missing: Any = converter.execute_one(select(_ItemRow.id, _ItemRow.label).where(_ItemRow.id == 999), _ItemDTO)

    assert first == {'id': 1, 'label': 'a'}
    assert missing is None


def test_label_renames_column_to_match_field_name() -> None:
    converter = SqlalchemyConverter(_seeded_session())

    statement = select(_ItemRow.id, _ItemRow.label.label('renamed'))
    rows: Any = converter.execute_all(statement, list[_ItemDTO])

    assert rows == [{'id': 1, 'renamed': 'a'}, {'id': 2, 'renamed': 'b'}]
