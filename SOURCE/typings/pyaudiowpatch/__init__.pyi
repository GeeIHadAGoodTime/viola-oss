from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, TypedDict

paContinue: int
paInt16: int
paWASAPI: int

class Stream(Protocol):
    def start_stream(self) -> None: ...
    def stop_stream(self) -> None: ...
    def close(self) -> None: ...

class PyAudio:
    def open(self, *args: object, **kwargs: object) -> Stream: ...
    def terminate(self) -> None: ...
    def get_host_api_info_by_type(self, host_api_type: int) -> HostApiInfo: ...
    def get_device_info_by_index(self, index: int) -> DeviceInfo: ...
    def get_loopback_device_info_generator(self) -> Iterable[DeviceInfo]: ...

class HostApiInfo(TypedDict):
    defaultOutputDevice: int

class DeviceInfo(TypedDict, total=False):
    index: int
    name: str
    defaultSampleRate: float
    maxInputChannels: int
