from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from .transformer.batcher import populate_context_with_batch


def validate_batch[ModelT: BaseModel](
    model: type[ModelT],
    rows: Sequence[Mapping[str, Any]],
) -> list[ModelT]:
    """Validate a page of rows against *model*, sharing a batch-aware context.

    Walks *rows* once to collect the id set referenced by each
    :class:`BatchArg`-aware transformer, then validates every row against
    that shared context. The first row's ``executor.fetch(...)`` inside a
    transformer issues the bulk query; subsequent rows hit the query
    executor's entity-level cache::

        class TeamDTO(BaseModel):
            id: int
            owner: OwnerTransformerAnnotated

        rows = session.execute(select(TeamRow)).mappings().all()
        teams = validate_batch(TeamDTO, rows)
    """
    context = populate_context_with_batch(model, rows)
    return [model.model_validate(row, context=context) for row in rows]
