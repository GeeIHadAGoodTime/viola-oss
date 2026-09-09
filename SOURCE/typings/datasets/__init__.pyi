from __future__ import annotations

from collections.abc import Iterable

def load_dataset(*args: object, **kwargs: object) -> Iterable[dict[str, object]]: ...
