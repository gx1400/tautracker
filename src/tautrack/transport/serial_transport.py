"""Serial transport.

pyserial is blocking, so every call is pushed to a worker thread to keep the event loop
free. The read timeout is deliberately short: :class:`Session` polls repeatedly while
scanning for a prompt, so a short timeout makes it responsive rather than slow.
"""

from __future__ import annotations

import asyncio

import serial

from .base import Transport, TransportError

_PARITY = {"none": serial.PARITY_NONE, "odd": serial.PARITY_ODD, "even": serial.PARITY_EVEN}
_BYTESIZE = {5: serial.FIVEBITS, 6: serial.SIXBITS, 7: serial.SEVENBITS, 8: serial.EIGHTBITS}
_STOPBITS = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}


class SerialTransport(Transport):
    def __init__(
        self,
        port: str,
        baudrate: int = 19200,
        bytesize: int = 7,
        parity: str = "odd",
        stopbits: int = 1,
    ) -> None:
        self._port = port
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._ser: serial.Serial | None = None

    @property
    def description(self) -> str:
        p = self._parity[0].upper()
        return f"serial:{self._port}@{self._baudrate}/{self._bytesize}{p}{self._stopbits}"

    async def open(self) -> None:
        def _open() -> serial.Serial:
            return serial.Serial(
                port=self._port,
                baudrate=self._baudrate,
                bytesize=_BYTESIZE[self._bytesize],
                parity=_PARITY[self._parity],
                stopbits=_STOPBITS[self._stopbits],
                # Short, so Session's prompt scan loop stays responsive.
                timeout=0.05,
            )

        try:
            self._ser = await asyncio.to_thread(_open)
        except Exception as exc:
            raise TransportError(f"cannot open {self._port}: {exc}") from exc

    async def close(self) -> None:
        if self._ser is not None:
            ser, self._ser = self._ser, None
            await asyncio.to_thread(ser.close)

    async def write(self, data: bytes) -> None:
        if self._ser is None:
            raise TransportError("serial port not open")
        try:
            await asyncio.to_thread(self._ser.write, data)
        except Exception as exc:
            raise TransportError(f"serial write failed: {exc}") from exc

    async def read(self, timeout: float) -> bytes:
        if self._ser is None:
            raise TransportError("serial port not open")

        def _read() -> bytes:
            assert self._ser is not None
            # read() honours the port timeout; in_waiting lets us take a whole chunk
            # when one is available instead of a byte at a time.
            n = self._ser.in_waiting
            return self._ser.read(n if n > 0 else 1)

        try:
            return await asyncio.wait_for(asyncio.to_thread(_read), timeout=max(timeout, 0.1))
        except TimeoutError:
            return b""
        except Exception as exc:
            raise TransportError(f"serial read failed: {exc}") from exc

    async def reset_input(self) -> None:
        if self._ser is not None:
            await asyncio.to_thread(self._ser.reset_input_buffer)
