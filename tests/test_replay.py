"""Replay transport (Q11): deterministic, hardware-free, and deliberately fragmented."""

from __future__ import annotations

import json

import pytest

from tautrack.device.session import Session
from tautrack.transport import ReplayTransport, TransportError


def fixture(tmp_path, records):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return p


async def test_replays_recorded_replies(tmp_path) -> None:
    p = fixture(tmp_path, [{"command": "*IDN?", "body": "ACME,X,1,2", "prompt": "scpi >"}])
    s = Session(ReplayTransport(p))
    await s.open()
    assert (await s.query("*IDN?")).body == "ACME,X,1,2"


async def test_replies_are_fragmented_across_reads(tmp_path) -> None:
    """Real hardware delivered the status screen in six segments.

    Serving it whole would let a prompt scanner that only inspects a single read pass
    its tests and then fail against the device.
    """
    body = "x" * 900
    p = fixture(tmp_path, [{"command": ":SYST:STAT?", "body": body, "prompt": "scpi >"}])
    s = Session(ReplayTransport(p, chunk_size=100))
    await s.open()
    reply = await s.query(":SYST:STAT?")
    assert reply.body == body
    assert reply.reads > 1


async def test_unknown_command_answers_like_the_firmware(tmp_path) -> None:
    """An undefined header yields E-113, keeping error handling on the tested path."""
    p = fixture(tmp_path, [{"command": "*IDN?", "body": "A,B,C,D", "prompt": "scpi >"}])
    s = Session(ReplayTransport(p))
    await s.open()
    reply = await s.query(":PTIM:TCODE?")
    assert reply.error_code == 113


async def test_stale_data_prompt_is_preserved(tmp_path) -> None:
    """E-230 must survive replay: it is how 1PPS TI behaves while unlocked."""
    p = fixture(tmp_path, [{"command": ":SYNC:TINT?", "body": "", "prompt": "E-230>"}])
    s = Session(ReplayTransport(p))
    await s.open()
    assert (await s.query(":SYNC:TINT?")).error_code == 230


async def test_sequence_loops_so_a_long_run_keeps_receiving_data(tmp_path) -> None:
    p = fixture(
        tmp_path,
        [
            {"command": ":SYNC:TFOM?", "body": "+3", "prompt": "scpi >"},
            {"command": ":SYNC:TFOM?", "body": "+4", "prompt": "scpi >"},
        ],
    )
    s = Session(ReplayTransport(p, loop=True))
    await s.open()
    seen = [(await s.query(":SYNC:TFOM?")).body for _ in range(4)]
    assert seen == ["+3", "+4", "+3", "+4"]


async def test_missing_fixture_is_a_clear_error(tmp_path) -> None:
    t = ReplayTransport(tmp_path / "nope.jsonl")
    with pytest.raises(TransportError, match="not found"):
        await t.open()


async def test_empty_fixture_is_rejected(tmp_path) -> None:
    p = tmp_path / "empty.jsonl"
    p.write_text("\n# only a comment\n", encoding="utf-8")
    with pytest.raises(TransportError, match="empty"):
        await ReplayTransport(p).open()


async def test_real_captured_session_drives_a_full_poll(session_fixture) -> None:
    """End-to-end against the captured device session, with no hardware."""
    if not session_fixture.exists():
        pytest.skip("session fixture not generated")
    s = Session(ReplayTransport(session_fixture))
    await s.open()
    ident = await s.identify()
    assert ident["model"] == "Z3801A"
    screen = await s.query(":SYST:STAT?")
    assert screen.ok
    assert "Receiver Status" in screen.body
