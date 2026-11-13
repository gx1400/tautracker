"""SCPI session: prompt scanning, the read-only allowlist, and error classification."""

from __future__ import annotations

import pytest

from tautrack.device.session import ALLOWED_COMMANDS, NotAllowedError, Session
from tautrack.transport.base import Transport, TransportError


class FakeTransport(Transport):
    """Serves canned replies, optionally split across several reads.

    Fragmentation is the point: real hardware delivered the status screen in six TCP
    segments, so a transport that always answers in one piece would let a broken
    prompt scanner pass.
    """

    def __init__(self, replies: dict[str, str], chunk: int = 7) -> None:
        self.replies = replies
        self.chunk = chunk
        self.pending = bytearray()
        self.written: list[str] = []
        self.opened = False

    @property
    def description(self) -> str:
        return "fake"

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.opened = False

    async def write(self, data: bytes) -> None:
        cmd = data.decode().strip()
        self.written.append(cmd)
        self.pending.extend(self.replies.get(cmd, "E-113> ").encode())

    async def read(self, timeout: float) -> bytes:
        if not self.pending:
            return b""
        take = min(self.chunk, len(self.pending))
        out = bytes(self.pending[:take])
        del self.pending[:take]
        return out

    async def reset_input(self) -> None:
        self.pending.clear()


async def test_reply_is_assembled_across_many_reads() -> None:
    body = "\n".join(f"line {i} of the status screen" for i in range(20))
    t = FakeTransport({":SYST:STAT?": body + "\r\nscpi > "}, chunk=5)
    s = Session(t)
    await s.open()
    reply = await s.query(":SYST:STAT?")
    assert reply.ok
    assert reply.body == body
    # A single read could not have produced this; the scanner must have looped.
    assert reply.reads > 1


async def test_prompt_split_across_two_reads_is_still_found() -> None:
    """The prompt itself can straddle a read boundary, so scan the whole buffer."""
    t = FakeTransport({"*IDN?": "ACME,THING,1,2\r\nscpi > "}, chunk=1)
    s = Session(t)
    await s.open()
    reply = await s.query("*IDN?")
    assert reply.ok
    assert reply.body == "ACME,THING,1,2"


async def test_error_prompt_marks_failure_and_exposes_the_code() -> None:
    t = FakeTransport({":SYNC:TINT?": "E-230> "})
    s = Session(t)
    await s.open()
    reply = await s.query(":SYNC:TINT?")
    assert not reply.ok
    # E-230 means recognised-but-stale; E-113 means the header does not exist. The
    # poller treats them very differently, so the distinction must survive.
    assert reply.error_code == 230


async def test_undefined_header_is_distinguishable_from_stale_data() -> None:
    # Uses an allowlisted command: queries the firmware rejects outright were left out
    # of the allowlist entirely, so they raise NotAllowedError before reaching the wire.
    t = FakeTransport({":PTIM:TCODE?": "E-113> "})
    s = Session(t)
    await s.open()
    reply = await s.query(":PTIM:TCODE?")
    assert reply.error_code == 113


async def test_timeout_without_a_prompt_raises() -> None:
    t = FakeTransport({"*IDN?": "no prompt here"})
    s = Session(t)
    await s.open()
    with pytest.raises(TransportError, match="no prompt"):
        await s.query("*IDN?", timeout=0.3)


@pytest.mark.parametrize(
    "command",
    [":SYST:PRES", ":GPS:POS N,1,2,3", ":GPS:INIT:DATE 2007,3,29", "*RST", ":DIAG:TEST"],
)
async def test_state_changing_commands_are_refused(command: str) -> None:
    """tautrack must be structurally incapable of reconfiguring the instrument (FR-7).

    ``:SYST:PRES`` on a real unit wipes the diagnostic log and the surveyed position,
    so a monitoring tool that can send it by accident is a liability.
    """
    s = Session(FakeTransport({}))
    await s.open()
    with pytest.raises(NotAllowedError):
        await s.query(command)


async def test_allowlist_contains_only_queries_and_cls() -> None:
    for cmd in ALLOWED_COMMANDS:
        assert cmd.endswith("?") or cmd == "*CLS", cmd


async def test_identify_splits_the_idn_reply() -> None:
    t = FakeTransport({"*IDN?": "HEWLETT-PACKARD,Z3801A,3542A05653,3543-A\r\nscpi > "})
    s = Session(t)
    await s.open()
    ident = await s.identify()
    assert ident == {
        "manufacturer": "HEWLETT-PACKARD",
        "model": "Z3801A",
        "serial": "3542A05653",
        "firmware": "3543-A",
    }


async def test_identify_tolerates_a_short_reply() -> None:
    """A non-HP unit may return fewer fields; that must not raise."""
    t = FakeTransport({"*IDN?": "ACME,THING\r\nscpi > "})
    s = Session(t)
    await s.open()
    ident = await s.identify()
    assert ident["manufacturer"] == "ACME"
    assert ident["firmware"] == ""
