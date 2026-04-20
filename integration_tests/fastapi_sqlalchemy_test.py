"""End-to-end HTTP + SQLAlchemy integration.

Drives the sample app from :mod:`sample_app` through real HTTP calls
against a SQLite in-memory database. Asserts both the rendered JSON
payload and the N+1 contract: a single bulk ``SELECT ... FROM users``
covers the whole page even when rows share owner ids.
"""

import pytest
from fastapi.testclient import TestClient
from sample_app import Base
from sample_app import TeamRow
from sample_app import UserRow
from sample_app import db_engine
from sample_app import fastapi_app
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session


@pytest.fixture()
def engine() -> Engine:
    Base.metadata.create_all(db_engine)
    with Session(db_engine) as session:
        session.add_all(
            [
                UserRow(id=10, name='u10'),
                UserRow(id=20, name='u20'),
                TeamRow(id=1, owner_id=10),
                TeamRow(id=2, owner_id=20),
                TeamRow(id=3, owner_id=10),
            ],
        )
        session.commit()
    return db_engine


@pytest.fixture()
def captured_sql(engine: Engine) -> list[str]:
    statements: list[str] = []

    @event.listens_for(engine, 'before_cursor_execute')
    def _capture(conn, cursor, statement, params, context, executemany) -> None:
        statements.append(statement)

    return statements


@pytest.fixture()
def client(engine: Engine, captured_sql: list[str]) -> TestClient:
    return TestClient(fastapi_app)


def test_http_route_returns_expected_payload(client: TestClient, captured_sql: list[str]) -> None:
    # Act
    response = client.get('/teams')

    # Assert
    assert response.status_code == 200
    assert response.json() == [
        {'id': 1, 'owner': {'id': 10, 'name': 'u10'}},
        {'id': 2, 'owner': {'id': 20, 'name': 'u20'}},
        {'id': 3, 'owner': {'id': 10, 'name': 'u10'}},
    ]
    # Assert — overlapping owner ids collapse into a single bulk SELECT against users.
    user_selects = [statement for statement in captured_sql if 'FROM users' in statement]
    assert len(user_selects) == 1
