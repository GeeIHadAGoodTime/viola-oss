from __future__ import annotations

import sys
from typing import TextIO


def console(
    *values: object,
    sep: str = " ",
    end: str = "\n",
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    """
    A `console()` replacement used to eliminate direct stdout printing across the codebase.

    - Writes to the provided stream (default: stdout)
    - Supports `sep`, `end`, `file`, `flush`
    - Best-effort unicode output on Windows consoles
    """

    stream = file if file is not None else sys.stdout
    if stream is None:
        # A windowed/frozen build (PyInstaller console=False) has no console:
        # sys.stdout and sys.stderr are None. console() is best-effort output,
        # so there is nowhere to write -- silently no-op rather than crash with
        # AttributeError. (CLAUDE.md: Viola never narrates failures; silent
        # recovery. Reproduced + gated by scripts/check_windowed_stream_safety.py.)
        return
    text = sep.join("" if v is None else str(v) for v in values) + end

    try:
        stream.write(text)
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "utf-8"
        data = text.encode(encoding, errors="replace")
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(data)
        else:
            stream.write(data.decode(encoding, errors="replace"))

    if flush:
        stream.flush()
