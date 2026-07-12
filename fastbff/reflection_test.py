"""Reflection under PEP 563 — string-annotation resolution end-to-end.

Regression tests for ``from __future__ import annotations``: when a user
enables PEP 563 in the module declaring queries, resolvers, or response
models, every annotation arrives as a *string* via ``inspect.signature(...)`` —
``param.annotation`` is ``'_FetchUsers'`` or ``'Annotated[_UserDTO | None,
Resolve(_FetchUsers)]'`` rather than the class. The reflection layer must
resolve those strings (via ``typing.get_type_hints``) so:

* ``Resolve`` / nested fields on the model are still discovered
  (``fastbff.resolve._introspect``), so ``render`` collects ids across the page
  and issues one bulk :class:`EntityQuery` fetch,
* ``Depends(...)`` parameters on handlers/resolvers are still picked up by
  finalize (``fastbff.di``) and injected into the synthesised
  ``provide_query_executor`` factory.

Targets are declared at module level — same as a real app — so PEP 563
string annotations resolve through the module's globals.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from inspect import signature
from typing import Annotated
from typing import get_args

from fastapi import Depends
from pydantic import BaseModel

from fastbff import EntityQuery
from fastbff import FastBFF
from fastbff import Query
from fastbff import QueryRouter
from fastbff import Resolve

_router = QueryRouter()

_db_calls: list[frozenset[int]] = []


class _UserDTO(BaseModel):
    id: int
    name: str = ''


class _FetchUsers(EntityQuery[int, _UserDTO]):
    ids: frozenset[int]


@_router.queries
def _fetch_users(query: _FetchUsers) -> dict[int, _UserDTO]:
    _db_calls.append(query.ids)
    return {i: _UserDTO(id=i, name=f'u{i}') for i in query.ids}


class _TeamDTO(BaseModel):
    id: int
    owner: Annotated[_UserDTO | None, Resolve(_FetchUsers)]


def test_render_pep563_module_issues_one_bulk_call() -> None:
    """Full plan → bulk-fetch → merge through a PEP 563-declared model + handler.

    The ``Resolve(_FetchUsers)`` field and the ``_FetchUsers`` entity query are
    both declared as string annotations; render must resolve them to collect
    ``{10, 20}`` and issue exactly one bulk call.
    """
    _db_calls.clear()
    app = FastBFF()
    app.include_router(_router)

    class _FetchTeams(Query[list[_TeamDTO]]):
        pass

    @app.queries(_FetchTeams)
    def _fetch_teams() -> list[dict[str, int]]:
        return [{'id': 1, 'owner': 10}, {'id': 2, 'owner': 20}, {'id': 3, 'owner': 10}]

    executor = app.finalize()()
    results = asyncio.run(executor.fetch(_FetchTeams()))

    assert _db_calls == [frozenset({10, 20})]
    assert [row.owner for row in results] == [
        _UserDTO(id=10, name='u10'),
        _UserDTO(id=20, name='u20'),
        _UserDTO(id=10, name='u10'),
    ]


@dataclass(frozen=True)
class _Marker:
    value: str


def _make_marker() -> _Marker:  # pragma: no cover - resolved by FastAPI, injected directly here
    return _Marker(value='unused')


class _FetchMarked(Query[str]):
    pass


def _fetch_marked(
    query: _FetchMarked,
    marker: Annotated[_Marker, Depends(_make_marker)],
) -> str:
    return marker.value


def test_finalize_picks_up_pep563_depends_param() -> None:
    """A ``Depends(...)`` param on a PEP 563 handler must still reach finalize.

    ``_fetch_marked``'s ``marker`` annotation arrives as the string
    ``'Annotated[_Marker, Depends(_make_marker)]'``; finalize resolves it against
    the module globals so the dep surfaces on ``provide_query_executor`` and is
    injected at dispatch.
    """
    app = FastBFF()
    app.queries(_FetchMarked)(_fetch_marked)

    provide_query_executor = app.finalize()

    (dep_param,) = signature(provide_query_executor).parameters.values()
    depends = get_args(dep_param.annotation)[1]
    assert depends.dependency is _make_marker

    executor = provide_query_executor(**{dep_param.name: _Marker(value='injected')})
    assert asyncio.run(executor.fetch(_FetchMarked())) == 'injected'
