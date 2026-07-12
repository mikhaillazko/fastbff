"""Integration: a *sync* FastAPI endpoint reaches an ``async def`` handler.

A sync endpoint injects :class:`SyncQueryExecutor`, whose ``fetch`` bridges the
async executor onto the event loop; the async handler then runs loop-native.
This drives the real Starlette stack through ``TestClient`` rather than
simulating the worker thread.
"""

from typing import Annotated

from fastapi import Depends
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from fastbff import FastBFF
from fastbff import Query
from fastbff import SyncQueryExecutor

app = FastBFF()


class _UserDTO(BaseModel):
    id: int
    name: str


class _FetchUser(Query[_UserDTO]):
    user_id: int


@app.queries
async def fetch_user(query: _FetchUser) -> _UserDTO:
    return _UserDTO(id=query.user_id, name=f'u{query.user_id}')


fastapi_app = FastAPI()


@fastapi_app.get('/user/{user_id}')
def get_user(
    user_id: int,
    sync_query_executor: Annotated[SyncQueryExecutor, Depends(SyncQueryExecutor)],
) -> _UserDTO:
    # Sync endpoint reaching an async handler: SyncQueryExecutor.fetch bridges
    # to the loop, the async handler runs loop-native. No extra code.
    return sync_query_executor.fetch(_FetchUser(user_id=user_id))


app.mount(fastapi_app)


def test_sync_endpoint_bridges_async_handler() -> None:
    client = TestClient(fastapi_app)

    response = client.get('/user/7')

    assert response.status_code == 200
    assert response.json() == {'id': 7, 'name': 'u7'}
