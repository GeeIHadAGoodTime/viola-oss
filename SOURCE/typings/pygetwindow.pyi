from __future__ import annotations

from typing import Protocol

class Window(Protocol):
    title: str
    left: int
    top: int
    width: int
    height: int

    def activate(self) -> None: ...

def getWindowsWithTitle(title: str) -> list[Window]: ...
