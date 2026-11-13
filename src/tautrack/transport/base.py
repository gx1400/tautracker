"""Transport abstraction and the read-until-prompt session.

The single most important behaviour here is **never using a fixed delay** (FR-2).
Measured against real hardware, ``:SYST:STAT?`` crossed a TCP socket in *six* separate
segments, so a single read returns a fragment and any sleep-then-read is guessing. The
reply is complete exactly when a SCPI prompt appears:

    ``scpi > ``   command succeeded
    ``E-nnn> ``   an error is queued - which also tells us the command failed, without
                  a separate round trip to find out

Measured timings on an HP Z3801A at 19200 7-O-1: short queries 0.20 s, status screen
2.84 s. The previous shell prototype used 0.7 s and 4.0 s fixed sleeps, so prompt
scanning is both correct and faster.
"""
from __future__ import annotations

import abc
import re
from dataclasses import dataclass

#: Terminates every reply. Captured so the caller can tell success from error.
PROMPT = re.compile(rb"(scpi >|E-\d+>)[ \t]*\r?\n?$")

#: Also match a prompt that has no trailing newline, which is how the device actually
#: leaves the line when it is waiting for input.
PROMPT_ANY = re.compile(rb"(scpi >|E-\d+>)[ \t]*$")


class TransportError(RuntimeError):
    """Transport failed: not open, read timed out, or the link dropped."""


@dataclass(slots=True)
class Reply:
    """One SCPI transaction's result."""

    command: str
    body: str
    prompt: str | None
    elapsed: float
    #: Number of distinct reads the reply took to assemble. Useful telemetry: it is
    #: direct evidence of fragmentation, and was 6 for the status screen over TCP.
    reads: int

    @property
    def ok(self) -> bool:
        return self.prompt is not None and self.prompt.startswith("scpi")

    @property
    def error_code(self) -> int | None:
        """SCPI error number from an ``E-nnn>`` prompt, if the command failed.

        Distinguishing these matters. ``E-113`` means the header does not exist on this
        firmware, so the query should never be retried. ``E-230`` means the command is
        recognised but its data is stale or unavailable right now - the query is valid
        and may succeed later, which is exactly the case for 1PPS time interval while
        the unit is in holdover.
        """
        if self.prompt is None or self.prompt.startswith("scpi"):
            return None
        m = re.match(r"E-(\d+)>", self.prompt)
        return int(m.group(1)) if m else None


class Transport(abc.ABC):
    """A byte pipe to the instrument.

    Deliberately narrow: open, close, write, read-some. All prompt handling lives in
    :class:`~tautrack.device.session.Session` so every transport behaves identically
    above this line, and so the replay transport needs no special handling anywhere
    else in the application.
    """

    @abc.abstractmethod
    async def open(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abc.abstractmethod
    async def read(self, timeout: float) -> bytes:
        """Return whatever is available, or ``b""`` if nothing arrived before timeout."""

    @abc.abstractmethod
    async def reset_input(self) -> None:
        """Discard buffered input so the next read starts at a clean boundary."""

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """Human-readable identity, for logs and telemetry attributes."""
