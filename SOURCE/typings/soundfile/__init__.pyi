from __future__ import annotations

from typing import Protocol

import numpy as np

class SupportsRead(Protocol):
    def read(self, size: int = ...) -> bytes: ...

def read(file: str) -> tuple[np.ndarray, int]: ...
def write(file: str, data: object, samplerate: int) -> None: ...
