from __future__ import annotations

from typing import Protocol, Sequence

class ServiceInfo:
    properties: dict[bytes, bytes]
    port: int

    def __init__(
        self,
        type_: str,
        name: str,
        addresses: Sequence[bytes] | None = ...,
        port: int = ...,
        properties: dict[str, str] | dict[bytes, bytes] | None = ...,
    ) -> None: ...
    def parsed_scoped_addresses(self) -> list[str]: ...

class ServiceListener(Protocol):
    def add_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None: ...
    def remove_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None: ...
    def update_service(self, zeroconf: Zeroconf, type_: str, name: str) -> None: ...

class Zeroconf:
    def get_service_info(self, type_: str, name: str) -> ServiceInfo | None: ...
    def register_service(self, info: ServiceInfo) -> None: ...
    def unregister_service(self, info: ServiceInfo) -> None: ...
    def close(self) -> None: ...

class ServiceBrowser:
    def __init__(
        self,
        zeroconf: Zeroconf,
        type_: str,
        listener: ServiceListener | None = ...,
    ) -> None: ...
    def cancel(self) -> None: ...
