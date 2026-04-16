"""Tests for ``populate_context_with_batch`` — Phase 1 "Plan" of Plan/Fetch/Merge."""

from dataclasses import dataclass
from typing import Annotated
from typing import Any

from pydantic import BaseModel

from src.transformer.batcher import populate_context_with_batch
from src.transformer.builder import build_transform_annotated
from src.transformer.decorators import bff_model
from src.transformer.registry import TransformerRegistry
from src.transformer.types import BatchArg


@dataclass(frozen=True)
class User:
    id: int
    name: str


UserId = int


def test_populate_context_collects_scalar_ids(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform(uid: UserId, batch: BatchArg[UserId]) -> User:
        return User(id=uid, name='')

    T = build_transform_annotated(transform)

    @bff_model
    class Row(BaseModel):
        owner: Annotated[User, T]

    batch_key = Row.__batches__[0].key  # type: ignore[attr-defined]
    rows: list[dict[str, Any]] = [{'owner': 1}, {'owner': 2}, {'owner': 2}]

    ctx = populate_context_with_batch(Row, rows)

    assert ctx == {batch_key: {1, 2}}


def test_populate_context_collects_iterable_ids_and_skips_none(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform(uid: UserId, batch: BatchArg[UserId]) -> list[User]:
        return []

    T = build_transform_annotated(transform)

    @bff_model
    class Row(BaseModel):
        owners: Annotated[list[User], T]

    batch_key = Row.__batches__[0].key  # type: ignore[attr-defined]
    rows: list[dict[str, Any]] = [{'owners': [1, 2, None]}, {'owners': [2, 3]}, {'owners': None}]

    ctx = populate_context_with_batch(Row, rows)

    assert ctx == {batch_key: {1, 2, 3}}


def test_populate_context_returns_empty_for_model_without_batches() -> None:
    @bff_model
    class Row(BaseModel):
        id: int

    ctx = populate_context_with_batch(Row, [{'id': 1}, {'id': 2}])
    assert ctx == {}
