"""Socket guard: offline bench never talks to the network."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager

from readyagents.errors import BenchRefused


class _BlockedSocket(socket.socket):
    def connect(self, *args: object, **kwargs: object) -> None:  # type: ignore[override]
        raise BenchRefused("offline bench blocked a network connect", reason="network")

    def connect_ex(self, *args: object, **kwargs: object) -> int:  # type: ignore[override]
        raise BenchRefused("offline bench blocked a network connect", reason="network")


def _blocked_create_connection(*args: object, **kwargs: object) -> None:
    raise BenchRefused("offline bench blocked a network connect", reason="network")


@contextmanager
def no_network() -> Iterator[None]:
    real_socket = socket.socket
    real_create = socket.create_connection
    socket.socket = _BlockedSocket  # type: ignore[misc, assignment]
    socket.create_connection = _blocked_create_connection  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket = real_socket  # type: ignore[misc]
        socket.create_connection = real_create
