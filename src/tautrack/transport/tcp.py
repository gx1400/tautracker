"""TCP transport, for a serial device server (Waveshare, ser2net, or the dev shim).

This is the intended long-term transport: it needs no device passthrough, so it works
identically whether the Docker daemon runs on Linux, Windows or macOS (NFR-2b).

Two operational notes that belong with the code, because both failure modes look like
instrument faults rather than configuration mistakes:

* A device server's **heartbeat and registration packet** features must be disabled.
  They inject bytes into a transparent stream, landing inside a reply and corrupting
  the parse intermittently.
* Only **one** client may connect. SCPI is request/response against shared device
  state, so concurrent readers consume each other's replies (C-2). The Waveshare will
  happily accept 30 connections; that is a trap, not a feature.
"""

from __future__ import annotations

import asyncio
import contextlib

from .base import Transport, TransportError


class TcpTransport(Transport):
    def __init__(self, host: str, port: int) -> None:
        self._host = host
        self._port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def description(self) -> str:
        return f"tcp:{self._host}:{self._port}"

    async def open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        except Exception as exc:
            raise TransportError(f"cannot connect to {self._host}:{self._port}: {exc}") from exc

    async def close(self) -> None:
        if self._writer is not None:
            writer, self._writer, self._reader = self._writer, None, None
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise TransportError("tcp not connected")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except Exception as exc:
            raise TransportError(f"tcp write failed: {exc}") from exc

    async def read(self, timeout: float) -> bytes:
        if self._reader is None:
            raise TransportError("tcp not connected")
        try:
            data = await asyncio.wait_for(self._reader.read(4096), timeout=max(timeout, 0.1))
        except TimeoutError:
            return b""
        except Exception as exc:
            raise TransportError(f"tcp read failed: {exc}") from exc
        if data == b"":
            # Clean EOF: the gateway closed the connection. Distinguish this from a
            # read timeout so the poller reconnects instead of spinning on empty reads.
            raise TransportError("tcp connection closed by peer")
        return data

    async def reset_input(self) -> None:
        """Drain anything already buffered.

        Matters after a reconnect or a skipped poll: a partial reply left in flight
        would otherwise be prepended to the next one and corrupt its parse.
        """
        if self._reader is None:
            return
        while True:
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=0.05)
            except (TimeoutError, Exception):
                return
            if not chunk:
                return
