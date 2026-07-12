"""Integration: a *sync* FastAPI endpoint reaches an ``async def`` handler.

This exercises the headline DX promise — a sync endpoint needs no extra code
because Starlette runs it in an anyio worker thread, where fastbff bridges the
async handler's coroutine onto the loop. Unlike the unit test that *simulates*
the worker thread with ``anyio.to_thread.run_sync``, this drives the real
Starlette stack through ``TestClient``.
"""

from typing import Annotated

from fastapi import Depends
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor

app = FastBFF()


class _UserDTO(BaseModel):
    id: int
    name: str


class _FetchUser(Query[_UserDTO]):
    user_id: int


@app.queries
async def fetch_user(args: _FetchUser) -> _UserDTO:
    return _UserDTO(id=args.user_id, name=f'u{args.user_id}')


fastapi_app = FastAPI()


@fastapi_app.get('/user/{user_id}')
def get_user(
    user_id: int,
    query_executor: Annotated[QueryExecutor, Depends(QueryExecutor)],
) -> _UserDTO:
    # Sync endpoint, async handler, plain ``fetch`` — no ``afetch``, no extra
    # code. Starlette runs this in a worker thread, so the bridge just works.
    return query_executor.fetch(_FetchUser(user_id=user_id))


app.mount(fastapi_app)


def test_sync_endpoint_bridges_async_handler() -> None:
    client = TestClient(fastapi_app)

    response = client.get('/user/7')

    assert response.status_code == 200
    assert response.json() == {'id': 7, 'name': 'u7'}
