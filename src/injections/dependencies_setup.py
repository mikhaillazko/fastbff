from collections.abc import Callable
from collections.abc import Iterator
from dataclasses import dataclass
from dataclasses import field
from typing import Any


@dataclass
class DependenciesSetup:
    """Registry of (interface, implementation) pairs for DI overrides."""

    _items: list[tuple[Callable[..., Any], Callable[..., Any]]] = field(default_factory=list)

    def register(self, interface: Callable[..., Any], interface_impl: Callable[..., Any]) -> None:
        item = (interface, interface_impl)
        self._items.append(item)

    def override(self, interface: Callable[..., Any], interface_impl: Callable[..., Any]) -> None:
        for idx, (original, _) in enumerate(self._items):
            if original is interface:
                self._items[idx] = (interface, interface_impl)
                break
        else:
            self.register(interface, interface_impl)

    def clone(self) -> 'DependenciesSetup':
        return DependenciesSetup(_items=self._items.copy())

    def __iter__(self) -> Iterator[tuple[Callable[..., Any], Callable[..., Any]]]:
        yield from self._items
