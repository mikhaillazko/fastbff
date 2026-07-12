from collections.abc import Callable
from functools import cache
from typing import Any
from typing import get_type_hints


@cache
def cached_type_hints(func: Callable) -> dict[str, Any]:
    """Return ``typing.get_type_hints(func, include_extras=True)`` or ``{}`` on failure.

    PEP 563 (``from __future__ import annotations``) leaves ``param.annotation``
    as a string; ``get_type_hints`` evaluates those strings against the
    function's module globals. We swallow resolution errors (e.g. closures
    referencing locals) so the caller can fall back to the raw annotation —
    which is already a real type when PEP 563 is not in effect.
    """
    try:
        return get_type_hints(func, include_extras=True)
    except Exception:
        return {}
