"""Tests for ``TransformerRegistry`` — registration + BatchArg discovery."""

from dataclasses import dataclass
from typing import Annotated
from typing import get_args
from typing import get_origin

import pytest
from pydantic_core.core_schema import ValidationInfo

from fastbff.exceptions import TransformerRegistrationError
from fastbff.transformer.registry import TransformerRegistry
from fastbff.transformer.registry import build_transform_annotated
from fastbff.transformer.registry import transformer_callable
from fastbff.transformer.registry import transformer_metadata
from fastbff.transformer.types import BatchArg
from fastbff.transformer.types import TransformerFieldInfo


@dataclass(frozen=True)
class User:
    id: int
    name: str


UserId = int


# ---------------------------------------------------------------------------
# Registration semantics
# ---------------------------------------------------------------------------


def test_registry_returns_original_function_with_attached_field_info(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user_id(user_id: UserId) -> User:
        return User(id=user_id, name=f'user:{user_id}')

    # Decorator returns the original function — directly callable.
    assert transform_user_id(user_id=3) == User(id=3, name='user:3')


def test_build_transform_annotated_returns_annotated_alias(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user_id(user_id: UserId) -> User:
        return User(id=user_id, name='')

    UserTransformerAnnotated = build_transform_annotated(transform_user_id)

    assert get_origin(UserTransformerAnnotated) is Annotated
    args = get_args(UserTransformerAnnotated)
    assert args[0] is User
    assert any(isinstance(meta, TransformerFieldInfo) for meta in args[1:])


def test_registry_raises_on_missing_return_type(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    def bad(value: int):  # noqa: ANN202
        return value

    with pytest.raises(TransformerRegistrationError, match='must have a return type annotation'):
        transformer(bad)


def test_build_transform_annotated_rejects_unregistered(noop_injector) -> None:
    def not_registered(value: int) -> int:
        return value

    with pytest.raises(TransformerRegistrationError, match='not a registered @transformer'):
        build_transform_annotated(not_registered)


def test_transformer_callable_returns_underlying_func(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user(user_id: UserId) -> User:
        return User(id=user_id, name=f'u:{user_id}')

    underlying = transformer_callable(transform_user)
    assert underlying is not None
    assert underlying(user_id=7) == User(id=7, name='u:7')


def test_transformer_metadata_finds_field_info_on_func_or_alias(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user(user_id: UserId) -> User:
        return User(id=user_id, name='')

    metadata_from_func = transformer_metadata(transform_user)
    assert isinstance(metadata_from_func, TransformerFieldInfo)
    assert metadata_from_func.return_type is User

    UserTransformerAnnotated = build_transform_annotated(transform_user)
    metadata_from_alias = transformer_metadata(UserTransformerAnnotated)
    assert metadata_from_alias is metadata_from_func


# ---------------------------------------------------------------------------
# BatchArg discovery + batch_fetch_type auto-derivation
# ---------------------------------------------------------------------------


def test_batch_arg_detected_with_plain_return_type(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user_id(user_id: UserId, users_batch: BatchArg[UserId]) -> User:
        return User(id=user_id, name=str(user_id))

    field_info = transformer_metadata(transform_user_id)
    assert field_info is not None
    assert field_info.batch_arg_name == 'users_batch'
    assert field_info.batch_fetch_type == dict[UserId, User]
    assert field_info.batch_key is not None
    assert field_info.batch_key.endswith('#users_batch')


def test_batch_fetch_type_unwraps_optional(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user(user_id: UserId, users_batch: BatchArg[UserId]) -> User | None:
        return None

    field_info = transformer_metadata(transform_user)
    assert field_info is not None
    assert field_info.batch_fetch_type == dict[UserId, User]


def test_batch_fetch_type_unwraps_list(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform_user(user_id: UserId, users_batch: BatchArg[UserId]) -> list[User]:
        return []

    field_info = transformer_metadata(transform_user)
    assert field_info is not None
    assert field_info.batch_fetch_type == dict[UserId, User]


def test_has_info_arg_detected(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def with_info(user_id: UserId, info: ValidationInfo) -> User:
        return User(id=user_id, name='')

    field_info = transformer_metadata(with_info)
    assert field_info is not None
    assert field_info.has_info_arg is True
