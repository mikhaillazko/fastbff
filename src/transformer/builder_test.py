"""Tests for ``build_transform_annotated`` — wrapping a transformer into a Pydantic field type."""

from dataclasses import dataclass
from typing import get_args
from typing import get_origin

from src.transformer.builder import build_transform_annotated
from src.transformer.registry import TransformerRegistry
from src.transformer.types import TransformerAnnotation


@dataclass(frozen=True)
class User:
    id: int
    name: str


UserId = int


def test_build_transform_annotated_wraps_in_annotated(noop_injector) -> None:
    transformer = TransformerRegistry(injector=noop_injector)

    @transformer
    def transform(user_id: UserId) -> User:
        return User(id=user_id, name='x')

    annotated = build_transform_annotated(transform)
    # Annotated[User, PlainValidator, PlainSerializer, TransformerAnnotation]
    assert get_origin(annotated) is not None
    args = get_args(annotated)
    assert args[0] is User
    assert any(isinstance(m, TransformerAnnotation) for m in args[1:])
