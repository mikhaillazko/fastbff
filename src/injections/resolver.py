from collections.abc import Callable
from contextlib import ExitStack
from contextlib import contextmanager
from typing import Any

from .dependant import Dependant
from .dependency_provider import DependencyProvider
from .types import DependenciesCache


def solve_dependencies(
    *,
    dependant: Dependant,
    dependency_cache: DependenciesCache,
    dependency_provider: DependencyProvider,
    stack: ExitStack,
) -> tuple[dict[str, Any], list[Any], dict[Callable[..., Any], Any]]:
    values: dict[str, Any] = {}
    errors: list[Any] = []
    for sub_dependant in dependant.dependencies:
        if sub_dependant.use_cache and sub_dependant.cache_key in dependency_cache:
            solved = dependency_cache[sub_dependant.cache_key]
            values[sub_dependant.name] = solved
            continue

        call = sub_dependant.call
        use_sub_dependant = sub_dependant
        original_call = sub_dependant.call
        if original_call in dependency_provider.dependency_overrides:
            call = dependency_provider.dependency_overrides[original_call]
            use_sub_dependant = Dependant(call=call)

        solved_result = solve_dependencies(
            dependant=use_sub_dependant,
            dependency_cache=dependency_cache,
            dependency_provider=dependency_provider,
            stack=stack,
        )
        sub_values, sub_errors, sub_dependency_cache = solved_result
        dependency_cache.update(sub_dependency_cache)
        if sub_errors:
            errors.extend(sub_errors)
            continue

        if use_sub_dependant.is_gen_callable:
            cm = contextmanager(call)(**sub_values)
            solved = stack.enter_context(cm)
        else:
            solved = call(**sub_values)

        assert sub_dependant.name, 'Dependant name must be set'
        values[sub_dependant.name] = solved
        if sub_dependant.cache_key not in dependency_cache:
            dependency_cache[sub_dependant.cache_key] = solved

    return values, errors, dependency_cache


def solve_dependency(
    *,
    type_: type,
    dependency_cache: DependenciesCache,
    dependency_provider: DependencyProvider,
    stack: ExitStack,
) -> tuple[Any, list[Any], dict[Callable[..., Any], Any]]:
    dependant = dependency_provider.get_dependant_impl(type_)
    values, errors, _ = solve_dependencies(
        dependant=dependant,
        stack=stack,
        dependency_cache=dependency_cache,
        dependency_provider=dependency_provider,
    )

    return dependant.call(**values), errors, dependency_cache
