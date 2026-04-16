from collections.abc import Callable
from typing import Any

DependenciesCache = dict[Callable[..., Any], Any]
