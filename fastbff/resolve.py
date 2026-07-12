"""The ``Resolve`` field annotation and the three-phase render pipeline.

Composition lives here, not in Pydantic validation. A response model declares
how to populate a relation field with one annotation::

    class TeamDTO(BaseModel):
        id: int
        owner: Annotated[UserDTO | None, Resolve(FetchUsers)]      # key = raw row['owner']

and :func:`render` turns a page of raw rows into validated models in three
phases owned by the executor (the DataLoader / GraphQL-executor separation):

1. **PLAN**  — walk raw rows once, collect the id-set per :class:`Resolve` field.
2. **FETCH** — one bulk call per field, all fields concurrent (``asyncio.gather``).
3. **MERGE** — substitute resolved values into rows, then ``model_validate`` each
   row (context-free, side-effect-free).

Nested models resolve depth-first: a field typed as another resolve-bearing
model re-enters the pipeline as its own batch, so the one-bulk-fetch-per-field
guarantee holds recursively.
"""

import asyncio
import types as builtin_types
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated
from typing import Any
from typing import Union
from typing import get_args
from typing import get_origin
from typing import get_type_hints

from pydantic import BaseModel

from fastbff.exceptions import ResolveRegistrationError

_RESOLVE_FIELDS_ATTR = '__fastbff_resolve_fields__'
_NESTED_FIELDS_ATTR = '__fastbff_nested_fields__'


class Resolve:
    """Field metadata declaring how a relation field is populated.

    Two forms, exactly one argument each:

    - ``Resolve(SomeEntityQuery)`` — the raw row value for this field is a key
      (or an iterable of keys) into ``SomeEntityQuery``'s ``dict[K, V]`` result.
      ``SomeEntityQuery`` must be a registered :class:`EntityQuery`.
    - ``Resolve(resolver=fn)`` — ``fn`` is a batch-first callable
      ``(ids, *deps) -> dict[key, value]`` (sync or ``async def``). It receives
      the collected id-set plus any ``QueryExecutor`` / ``Depends(...)`` params
      injected the same way a ``@queries`` handler's are.

    ``Resolve`` is inert Pydantic metadata — it carries no core schema, so a
    model with ``Resolve`` fields validates as a plain model once the resolved
    values have been substituted in.
    """

    __slots__ = ('query_type', 'resolver')

    def __init__(self, query_type: Any = None, *, resolver: Callable[..., Any] | None = None) -> None:
        if (query_type is None) == (resolver is None):
            raise ResolveRegistrationError(
                'Resolve(...) takes exactly one of a query type or resolver=<fn> — '
                'e.g. Resolve(FetchUsers) or Resolve(resolver=resolve_owner).',
            )
        self.query_type = query_type
        self.resolver = resolver

    def __repr__(self) -> str:
        if self.resolver is not None:
            name = getattr(self.resolver, '__name__', repr(self.resolver))
            return f'Resolve(resolver={name})'
        return f'Resolve({getattr(self.query_type, "__name__", self.query_type)})'


@dataclass(frozen=True, slots=True)
class ResolveField:
    name: str
    resolve: Resolve
    is_collection: bool


@dataclass(frozen=True, slots=True)
class NestedField:
    name: str
    model: type[BaseModel]
    is_collection: bool


def _strip_optional(annotation: Any) -> Any:
    """Return the single non-None member of an Optional/Union, else *annotation*."""
    origin = get_origin(annotation)
    if origin is Union or isinstance(annotation, builtin_types.UnionType):
        non_none = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    return annotation


def _analyze(annotation: Any) -> tuple[Any, bool]:
    """Reduce a field annotation to ``(core_type, is_collection)``.

    Strips a leading ``Annotated[...]``, then ``Optional``, then a single
    ``list``/``set``/``tuple``/``Sequence`` layer (reporting it as a collection).
    """
    if get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    annotation = _strip_optional(annotation)
    origin = get_origin(annotation)
    if origin in (list, set, frozenset, tuple, Sequence):
        args = get_args(annotation)
        element = _strip_optional(args[0]) if args else Any
        return element, True
    return annotation, False


def _find_resolve(metadata: Iterable[Any]) -> Resolve | None:
    resolves = [meta for meta in metadata if isinstance(meta, Resolve)]
    if not resolves:
        return None
    if len(resolves) > 1:
        raise ResolveRegistrationError(
            f'A field declares multiple Resolve(...) annotations; only one is allowed: {resolves!r}',
        )
    return resolves[0]


def _introspect(model: type[BaseModel]) -> tuple[list[ResolveField], list[NestedField]]:
    """Compute the resolve + nested field lists for *model* and cache them on it.

    Uses ``get_type_hints(..., include_extras=True)`` so PEP 563 string
    annotations resolve while ``Annotated[...]`` metadata is preserved. Reads
    ``model.__dict__`` for the cache so a subclass is never short-circuited by a
    parent's cached lists.
    """
    resolve_fields: list[ResolveField] = []
    nested_fields: list[NestedField] = []
    hints = get_type_hints(model, include_extras=True)
    for field_name, field_type in hints.items():
        if field_name.startswith('__'):
            continue
        metadata = get_args(field_type)[1:] if get_origin(field_type) is Annotated else ()
        resolve = _find_resolve(metadata)
        if resolve is not None:
            _, is_collection = _analyze(field_type)
            resolve_fields.append(ResolveField(name=field_name, resolve=resolve, is_collection=is_collection))
            continue
        core, is_collection = _analyze(field_type)
        if isinstance(core, type) and issubclass(core, BaseModel) and model_has_resolve(core):
            nested_fields.append(NestedField(name=field_name, model=core, is_collection=is_collection))

    setattr(model, _RESOLVE_FIELDS_ATTR, resolve_fields)
    setattr(model, _NESTED_FIELDS_ATTR, nested_fields)
    return resolve_fields, nested_fields


def _cached(model: type[BaseModel], attr: str, index: int) -> list:
    cached = model.__dict__.get(attr)
    if cached is None:
        cached = _introspect(model)[index]
    return cached


def get_resolve_fields(model: type[BaseModel]) -> list[ResolveField]:
    return _cached(model, _RESOLVE_FIELDS_ATTR, 0)


def get_nested_fields(model: type[BaseModel]) -> list[NestedField]:
    return _cached(model, _NESTED_FIELDS_ATTR, 1)


def model_has_resolve(model: Any) -> bool:
    """Whether *model* needs the render pipeline (has resolve or nested fields)."""
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        return False
    return bool(get_resolve_fields(model) or get_nested_fields(model))


def iter_resolves(model: type[BaseModel], _seen: set[type] | None = None) -> Iterable[Resolve]:
    """Yield every :class:`Resolve` reachable from *model*, recursing into nested models.

    Used at finalize time to discover resolver functions (so their ``Depends``
    params join the DI union) and to validate ``Resolve(QueryType)`` targets.
    """
    if _seen is None:
        _seen = set()
    if model in _seen:
        return
    _seen.add(model)
    for field in get_resolve_fields(model):
        yield field.resolve
    for field in get_nested_fields(model):
        yield from iter_resolves(field.model, _seen)


def classify_render(target: Any) -> tuple[str, type[BaseModel]] | None:
    """Decide whether *target* (``Query[T].T``) should go through :func:`render`.

    * ``('list', Model)`` for ``list[Model]`` where Model needs rendering.
    * ``('single', Model)`` for a bare ``Model`` that needs rendering.
    * ``None`` for anything else (``dict[K, V]``, primitives, unions, models
      without resolve fields).
    """
    if get_origin(target) is list:
        args = get_args(target)
        if args and model_has_resolve(args[0]):
            return ('list', args[0])
        return None
    if model_has_resolve(target):
        return ('single', target)
    return None


async def render(model: type[BaseModel], rows: Sequence[Any], executor: Any) -> list[Any]:
    """Turn a page of raw *rows* into validated *model* instances (three phases)."""
    row_list = list(rows)
    if not row_list:
        return []
    if isinstance(row_list[0], model):
        # Handler built the models itself — nothing to resolve.
        return row_list

    resolve_fields = get_resolve_fields(model)
    nested_fields = get_nested_fields(model)
    if not resolve_fields and not nested_fields:
        return [model.model_validate(row) for row in row_list]

    dict_rows = [dict(row) for row in row_list]

    tasks = [_render_resolve(field, dict_rows, executor) for field in resolve_fields]
    tasks += [_render_nested(field, dict_rows, executor) for field in nested_fields]
    columns = await asyncio.gather(*tasks)

    all_fields = [*resolve_fields, *nested_fields]
    for field, column in zip(all_fields, columns, strict=True):
        for row, value in zip(dict_rows, column, strict=True):
            row[field.name] = value

    return [model.model_validate(row) for row in dict_rows]


def _is_key_iterable(value: Any) -> bool:
    return isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping))


async def _render_resolve(field: ResolveField, rows: list[dict[str, Any]], executor: Any) -> list[Any]:
    """Phase 1+2 for one resolve field → a per-row list of substituted values."""
    ids: set[Any] = set()
    for row in rows:
        raw = row.get(field.name)
        if raw is None:
            continue
        if field.is_collection and _is_key_iterable(raw):
            ids.update(key for key in raw if key is not None)
        else:
            ids.add(raw)

    result_map = await executor.resolve_ids(field.resolve, frozenset(ids)) if ids else {}

    column: list[Any] = []
    for row in rows:
        raw = row.get(field.name)
        if field.is_collection:
            if raw is None or not _is_key_iterable(raw):
                column.append([])
            else:
                column.append([result_map[key] for key in raw if key in result_map])
        else:
            column.append(result_map.get(raw) if raw is not None else None)
    return column


async def _render_nested(field: NestedField, rows: list[dict[str, Any]], executor: Any) -> list[Any]:
    """Batch-render a nested resolve-bearing model field across the whole page."""
    if field.is_collection:
        flat: list[Any] = []
        spans: list[tuple[int, int]] = []
        for row in rows:
            raw = row.get(field.name) or []
            start = len(flat)
            flat.extend(raw)
            spans.append((start, len(flat)))
        rendered = await render(field.model, flat, executor)
        return [rendered[start:end] for start, end in spans]

    subrows: list[Any] = []
    slots: list[int | None] = []
    for row in rows:
        raw = row.get(field.name)
        if raw is None:
            slots.append(None)
        else:
            slots.append(len(subrows))
            subrows.append(raw)
    rendered = await render(field.model, subrows, executor)
    return [rendered[slot] if slot is not None else None for slot in slots]


async def apply_render(result: Any, render_info: tuple[str, type[BaseModel]], executor: Any) -> Any:
    """Run :func:`render` for a ``classify_render`` outcome on a handler result."""
    kind, model_cls = render_info
    if kind == 'list':
        rows = result if isinstance(result, list) else list(result)
        return await render(model_cls, rows, executor)
    rendered = await render(model_cls, [result], executor)
    return rendered[0]
