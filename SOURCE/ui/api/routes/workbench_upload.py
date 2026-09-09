"""Shared Workbench upload validation helpers."""

from __future__ import annotations

from fastapi import UploadFile

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


async def read_workbench_upload(file: UploadFile) -> bytes:
    """Read an upload with the Workbench service size cap enforced."""

    from services.workbench.dir import MAX_WORKBENCH_UPLOAD_BYTES

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_WORKBENCH_UPLOAD_BYTES:
            raise ValueError("workbench upload exceeds maximum size")
        chunks.append(chunk)
    return b"".join(chunks)


__all__ = ["read_workbench_upload"]
