from __future__ import annotations

from typing import Protocol, TypedDict, overload

class Callback(Protocol):
    def __call__(self, indata: object, frames: int, time_info: object, status: object) -> None: ...

class SupportsToBytes(Protocol):
    def tobytes(self) -> bytes: ...

@overload
def query_devices() -> list[DeviceInfo]: ...
@overload
def query_devices(device: int) -> DeviceInfo: ...

class DeviceInfo(TypedDict, total=False):
    name: str
    default_samplerate: float
    default_low_output_latency: float
    max_input_channels: int
    max_output_channels: int
    hostapi: int

class HostApiInfo(TypedDict, total=False):
    name: str
    default_output_device: int

class InputStream:
    def __init__(
        self,
        *,
        device: int,
        samplerate: float,
        channels: int,
        dtype: str,
        callback: Callback,
        blocksize: int,
    ) -> None: ...
    def start(self) -> None: ...
    def close(self) -> None: ...

def query_hostapis() -> list[HostApiInfo]: ...
def rec(
    frames: int,
    *,
    samplerate: float,
    channels: int,
    dtype: str,
    device: int | None = ...,
) -> SupportsToBytes: ...
def play(
    data: object,
    *,
    samplerate: float,
    device: int | None = ...,
) -> None: ...
def wait() -> None: ...
