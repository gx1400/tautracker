"""SCPI session: one transaction at a time, terminated by a prompt.

Two invariants are enforced here rather than trusted to callers:

1. **Read until the prompt, never on a timer** (FR-2). Measured on real hardware, the
   status screen arrived in six TCP segments.
2. **Queries only** (FR-7). Every command must be on an allowlist, so the application
   structurally cannot send ``:SYST:PRES``, ``:GPS:POS`` or ``:GPS:INIT:*``. A
   monitoring tool that can factory-reset the instrument it monitors is a liability -
   doing that by accident would wipe the diagnostic log and the surveyed position.
"""

from __future__ import annotations

import asyncio
import time

from ..transport import PROMPT_ANY, Reply, Transport, TransportError

#: Commands this application may transmit. Everything is a query except ``*CLS``,
#: which clears the error queue and changes no instrument configuration.
#:
#: Supported-vs-rejected status was established by probing firmware 3543-A; see
#: docs/command-map.md. Queries known to be rejected are deliberately absent.
ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "*CLS",
        "*IDN?",
        ":SYST:ERR?",
        ":SYST:STAT?",
        ":SYNC:TFOM?",
        ":SYNC:FFOM?",
        ":SYNC:STAT?",
        ":SYNC:HOLD:DUR?",
        ":SYNC:TINT?",
        ":PTIM:TINT?",
        ":PTIM:DATE?",
        ":PTIM:TIME?",
        ":PTIM:TCODE?",
        ":PTIM:LEAP:STAT?",
        ":PTIM:LEAP:ACC?",
        ":GPS:SAT:TRAC?",
        ":GPS:SAT:TRAC:COUN?",
        ":GPS:POS?",
        ":GPS:REF:ADEL?",
        ":GPS:ADEL?",
        ":DIAG:ROSC:EFC?",
        ":DIAG:ROSC:EFC:REL?",
        ":DIAG:ROSC:EFC:ABS?",
        ":DIAG:LOG:COUN?",
        ":OUTP:STAT?",
    }
)

#: Error codes worth distinguishing. E-113 is permanent (the header does not exist on
#: this firmware) so the query should be dropped; E-230 is transient (recognised
#: command, no valid data right now) so it should be retried later. 1PPS time interval
#: returns E-230 during holdover, which is why conflating the two would be wrong.
ERR_UNDEFINED_HEADER = 113
ERR_DATA_STALE = 230


class NotAllowedError(RuntimeError):
    """A command outside the read-only allowlist was attempted."""


class Session:
    """Serialised request/response access to one instrument.

    A lock guards the transport because SCPI has no request IDs: two overlapping
    transactions would each read part of the other's reply. This is the same hazard
    that makes multiple TCP clients unsafe (C-2), applied inside one process.
    """

    def __init__(self, transport: Transport, read_timeout: float = 8.0) -> None:
        self._t = transport
        self._timeout = read_timeout
        self._lock = asyncio.Lock()

    @property
    def description(self) -> str:
        return self._t.description

    async def open(self) -> None:
        await self._t.open()

    async def close(self) -> None:
        await self._t.close()

    async def query(self, command: str, timeout: float | None = None) -> Reply:
        cmd = command.strip()
        if cmd.upper() not in {c.upper() for c in ALLOWED_COMMANDS}:
            raise NotAllowedError(
                f"{cmd!r} is not in the read-only allowlist; tautrack never writes "
                f"instrument configuration (FR-7)"
            )
        async with self._lock:
            return await self._transact(cmd, timeout or self._timeout)

    async def _transact(self, cmd: str, timeout: float) -> Reply:
        await self._t.write(cmd.encode("ascii") + b"\r\n")
        buf = bytearray()
        reads = 0
        started = time.monotonic()
        deadline = started + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            chunk = await self._t.read(min(0.5, max(remaining, 0.05)))
            if chunk:
                buf.extend(chunk)
                reads += 1
                # Scan the accumulated buffer, not just this chunk: a prompt can be
                # split across reads.
                stripped = bytes(buf).rstrip(b" \t\r\n")
                m = PROMPT_ANY.search(stripped)
                if m:
                    body = stripped[: m.start()].decode("ascii", errors="replace").strip()
                    # The device echoes nothing, but a reply may repeat the prompt from
                    # a previous transaction if buffers were not clean; keep the last.
                    return Reply(
                        command=cmd,
                        body=body,
                        prompt=m.group(1).decode(),
                        elapsed=time.monotonic() - started,
                        reads=reads,
                    )

        raise TransportError(
            f"no prompt within {timeout:.1f}s for {cmd!r} after {reads} read(s), {len(buf)} byte(s)"
        )

    async def clear(self) -> None:
        """Clear the error queue and discard buffered input.

        Worth doing before a batch of queries: a stale ``E-nnn>`` left in the queue
        makes the next reply's prompt misleading about which command actually failed.
        """
        await self._t.reset_input()
        await self.query("*CLS")

    async def identify(self) -> dict[str, str]:
        """Read ``*IDN?`` once per connection, for resource attributes (FR-5)."""
        reply = await self.query("*IDN?")
        parts = [p.strip() for p in reply.body.split(",")]
        keys = ("manufacturer", "model", "serial", "firmware")
        return dict(zip(keys, parts + [""] * (len(keys) - len(parts)), strict=False))
