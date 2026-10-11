"""Exercise generated dependency signatures through FastAPI's public API."""

from collections.abc import AsyncIterator
from collections.abc import Iterator
from inspect import signature
from typing import Annotated
from typing import Any

import pytest
from fastapi import Depends
from fastapi import FastAPI
from fastapi import Security
from fastapi.security import SecurityScopes
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryExecutor


def _client(bff: FastBFF, query: Query[Any]) -> TestClient:
    api = FastAPI()

    @api.get('/')
    async def endpoint(executor: Annotated[QueryExecutor, Depends(QueryExecutor)]) -> Any:
        return await executor.fetch(query)

    bff.mount(api)
    return TestClient(api, raise_server_exceptions=False)


def test_implicit_dependency_factories_remain_distinct() -> None:
    class _A:
        pass

    class _B:
        pass

    class _FetchTypes(Query[list[str]]):
        pass

    bff = FastBFF()

    @bff.queries(_FetchTypes)
    async def fetch_types(a: Annotated[_A, Depends()], b: Annotated[_B, Depends()]) -> list[str]:
        return [type(a).__name__, type(b).__name__]

    with _client(bff, _FetchTypes()) as client:
        response = client.get('/')

    assert response.status_code == 200
    assert response.json() == ['_A', '_B']


@pytest.mark.parametrize('use_cache', [True, False])
def test_fastapi_owns_caching_across_parameters_handlers_and_requests(use_cache: bool) -> None:
    calls: list[int] = []
    root_calls: list[int] = []

    async def root() -> int:
        root_calls.append(len(root_calls) + 1)
        return root_calls[-1]

    async def factory(parent: Annotated[int, Depends(root)]) -> int:
        calls.append(len(calls) + 1)
        return calls[-1]

    class _FetchFirst(Query[list[int]]):
        pass

    class _FetchSecond(Query[int]):
        pass

    bff = FastBFF()

    @bff.queries(_FetchFirst)
    async def first(
        a: Annotated[int, Depends(factory, use_cache=use_cache)],
        b: Annotated[int, Depends(factory, use_cache=use_cache)],
        executor: QueryExecutor,
    ) -> list[int]:
        return [a, b, await executor.fetch(_FetchSecond())]

    @bff.queries(_FetchSecond)
    async def second(c: Annotated[int, Depends(factory, use_cache=use_cache)]) -> int:
        return c

    with _client(bff, _FetchFirst()) as client:
        first_response = client.get('/')
        second_response = client.get('/')

    assert first_response.status_code == second_response.status_code == 200
    assert first_response.json() == ([1, 1, 1] if use_cache else [1, 2, 3])
    assert second_response.json() == ([2, 2, 2] if use_cache else [4, 5, 6])
    assert len(calls) == (2 if use_cache else 6)
    assert root_calls == [1, 2]


def test_security_dependencies_preserve_each_occurrences_scopes() -> None:
    async def scopes(security_scopes: SecurityScopes) -> list[str]:
        return security_scopes.scopes

    class _FetchScopes(Query[list[list[str]]]):
        pass

    bff = FastBFF()

    @bff.queries(_FetchScopes)
    async def fetch_scopes(
        read: Annotated[list[str], Security(scopes, scopes=['read'])],
        write: Annotated[list[str], Security(scopes, scopes=['write'])],
    ) -> list[list[str]]:
        return [read, write]

    with _client(bff, _FetchScopes()) as client:
        response = client.get('/')

    assert response.status_code == 200
    assert response.json() == [['read'], ['write']]


@pytest.mark.parametrize('async_dependency', [True, False])
@pytest.mark.parametrize('handler_fails', [True, False])
def test_uncached_yield_dependencies_each_clean_up(async_dependency: bool, handler_fails: bool) -> None:
    events: list[str] = []

    def resource() -> Iterator[int]:
        value = len(events) + 1
        events.append(f'open:{value}')
        try:
            yield value
        finally:
            events.append(f'close:{value}')

    async def async_resource() -> AsyncIterator[int]:
        with_resource = resource()
        try:
            yield next(with_resource)
        finally:
            with_resource.close()

    factory = async_resource if async_dependency else resource

    class _FetchResources(Query[list[int]]):
        pass

    bff = FastBFF()

    @bff.queries(_FetchResources)
    async def fetch_resources(
        a: Annotated[int, Depends(factory, use_cache=False)],
        b: Annotated[int, Depends(factory, use_cache=False)],
    ) -> list[int]:
        events.append('handler')
        if handler_fails:
            raise RuntimeError('handler failed')
        return [a, b]

    with _client(bff, _FetchResources()) as client:
        response = client.get('/')

    assert response.status_code == (500 if handler_fails else 200)
    if not handler_fails:
        assert response.json() == [1, 2]
    assert events == ['open:1', 'open:2', 'handler', 'close:2', 'close:1']


@pytest.mark.skipif('scope' not in signature(Depends).parameters, reason='FastAPI does not support yield scopes')
def test_yield_scopes_keep_distinct_values_and_cleanup_boundaries() -> None:
    events: list[str] = []

    async def resource() -> AsyncIterator[int]:
        value = len(events) + 1
        events.append(f'open:{value}')
        try:
            yield value
        finally:
            events.append(f'close:{value}')

    class _FetchResources(Query[list[int]]):
        pass

    bff = FastBFF()

    @bff.queries(_FetchResources)
    async def fetch_resources(
        function: Annotated[int, Depends(resource, scope='function')],
        request: Annotated[int, Depends(resource, scope='request')],
    ) -> list[int]:
        events.append('handler')
        return [function, request]

    api = FastAPI()

    @api.get('/')
    async def endpoint(executor: Annotated[QueryExecutor, Depends(QueryExecutor)]) -> StreamingResponse:
        values = await executor.fetch(_FetchResources())

        async def body() -> AsyncIterator[str]:
            events.append('body')
            yield str(values)

        return StreamingResponse(body())

    bff.mount(api)
    with TestClient(api) as client:
        response = client.get('/')

    assert response.status_code == 200
    assert response.text == '[1, 2]'
    assert events == ['open:1', 'open:2', 'handler', 'close:1', 'body', 'close:2']
