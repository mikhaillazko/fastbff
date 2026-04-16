"""Tests for ``TransformerRegistry`` — registration + BatchArg discovery."""

from dataclasses import dataclass

import pytest
from pydantic_core.core_schema import ValidationInfo

from src.transformer.registry import TransformerRegistry
from src.transformer.types import _TRANSFORMER_ATTR
from src.transformer.types import BatchArg
from src.transformer.types import TransformerAnnotation


@dataclass(frozen=True)
class User:
    id: int
    name: str


UserId = int


# ---------------------------------------------------------------------------
# Registration semantics
# ---------------------------------------------------------------------------


def test_registry_attaches_transformer_annotation_to_wrapper(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user_id(user_id: UserId) -> User:
        return User(id=user_id, name=f'user:{user_id}')

    annotation = getattr(transform_user_id, _TRANSFORMER_ATTR, None)
    assert isinstance(annotation, TransformerAnnotation)
    assert annotation.return_type is User
    assert annotation.batch_arg_name is None
    assert annotation.batch_fetch_type is None


def test_registry_raises_on_missing_return_type(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    def bad(x: int):  # noqa: ANN202
        return x

    with pytest.raises(TypeError, match='must have a return type annotation'):
        transformer(bad)


# ---------------------------------------------------------------------------
# BatchArg discovery + batch_fetch_type auto-derivation
# ---------------------------------------------------------------------------


def test_batch_arg_detected_with_plain_return_type(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user_id(user_id: UserId, users_batch: BatchArg[UserId]) -> User:
        return User(id=user_id, name=str(user_id))

    annotation: TransformerAnnotation = getattr(transform_user_id, _TRANSFORMER_ATTR)
    assert annotation.batch_arg_name == 'users_batch'
    assert annotation.batch_fetch_type == dict[UserId, User]
    # batch_key is stable per (callable, arg-name)
    assert annotation.batch_key is not None
    assert annotation.batch_key.endswith('#users_batch')


def test_batch_fetch_type_unwraps_optional(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform(user_id: UserId, users_batch: BatchArg[UserId]) -> User | None:
        return None

    annotation: TransformerAnnotation = getattr(transform, _TRANSFORMER_ATTR)
    assert annotation.batch_fetch_type == dict[UserId, User]


def test_batch_fetch_type_unwraps_list(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform(user_id: UserId, users_batch: BatchArg[UserId]) -> list[User]:
        return []

    annotation: TransformerAnnotation = getattr(transform, _TRANSFORMER_ATTR)
    assert annotation.batch_fetch_type == dict[UserId, User]


def test_has_info_arg_detected(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def with_info(user_id: UserId, info: ValidationInfo) -> User:
        return User(id=user_id, name='')

    annotation: TransformerAnnotation = getattr(with_info, _TRANSFORMER_ATTR)
    assert annotation.has_info_arg is True
