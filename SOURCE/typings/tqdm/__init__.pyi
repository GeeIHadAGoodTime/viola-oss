from collections.abc import Iterable
from typing import Protocol, TypeVar

T = TypeVar("T")

class Tqdm(Protocol):
    def update(self, n: int = 1) -> None: ...
    def close(self) -> None: ...

def tqdm(
    iterable: Iterable[T] | None = ...,
    *,
    total: int | None = ...,
    desc: str = ...,
    unit: str = ...,
) -> Tqdm: ...
