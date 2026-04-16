"""Tests for ``@bff_model`` and ``introspect_model_transformers``."""

from dataclasses import dataclass
from typing import Annotated

import pytest
from pydantic import BaseModel

from src.transformer.builder import build_transform_annotated
from src.transformer.decorators import bff_model
from src.transformer.inspection import introspect_model_transformers
from src.transformer.registry import TransformerRegistry
from src.transformer.types import _BATCHES_ATTR
from src.transformer.types import BatchArg
from src.transformer.types import BatchInfo


@dataclass(frozen=True)
class User:
    id: int
    name: str


UserId = int


def test_bff_model_attaches_batches_when_transformer_field_present(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user(user_id: UserId, batch: BatchArg[UserId]) -> User:
        return User(id=user_id, name='')

    UserT = build_transform_annotated(transform_user)

    @bff_model
    class TeamDTO(BaseModel):
        id: int
        owner: Annotated[User, UserT]

    batches = getattr(TeamDTO, _BATCHES_ATTR, [])
    assert len(batches) == 1
    assert isinstance(batches[0], BatchInfo)
    assert batches[0].field_name == 'owner'
    assert batches[0].batch_fetch_type == dict[UserId, User]


def test_bff_model_noop_when_no_transformer_fields() -> None:
    @bff_model
    class PlainDTO(BaseModel):
        id: int
        name: str

    assert not hasattr(PlainDTO, _BATCHES_ATTR)


def test_bff_model_rejects_non_basemodel() -> None:
    with pytest.raises(AssertionError, match='Pydantic BaseModel'):

        @bff_model
        class NotAModel:
            pass


def test_introspect_model_transformers_is_idempotent(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform(uid: UserId, batch: BatchArg[UserId]) -> User:
        return User(id=uid, name='')

    T = build_transform_annotated(transform)

    class DTO(BaseModel):
        owner: Annotated[User, T]

    introspect_model_transformers(DTO)
    first = list(DTO.__batches__)  # type: ignore[attr-defined]
    introspect_model_transformers(DTO)
    second = list(DTO.__batches__)  # type: ignore[attr-defined]
    assert [b.field_name for b in first] == [b.field_name for b in second]
