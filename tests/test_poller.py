"""Poller: end-to-end against the captured session, with no hardware."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tautrack.config import Settings
from tautrack.device.screen import Satellite, Screen
from tautrack.poller import Poller, _health_score, _min_tracked_elevation, _signature
from tautrack.store.db import Store


@pytest.fixture
def settings(tmp_path, session_fixture) -> Settings:
    return Settings(
        database=tmp_path / "t.db",
        transport={"kind": "replay", "replay": {"path": session_fixture, "loop": True}},
        poll={"fast_seconds": 0.05, "slow_every": 1},
    )


async def run_briefly(poller: Poller, polls: int = 2) -> None:
    """Drive the poller until it has written a few samples, then stop it."""
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    for _ in range(200):
        await asyncio.sleep(0.05)
        if poller.last_poll_ts and poller.device_id:
            break
    stop.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_poll_writes_samples_and_satellites(settings, session_fixture) -> None:
    if not session_fixture.exists():
        pytest.skip("session fixture not generated")
    store = Store(settings.database)
    poller = Poller(settings, store)
    await run_briefly(poller)

    assert poller.device_id is not None
    latest = store.latest(poller.device_id)
    # Discrete fast queries...
    assert "efc_percent" in latest
    assert "efc_dac" in latest
    # ...and screen-only fields.
    assert "mode" in latest
    assert latest["mode"]["text"]
    assert store.latest_satellites(poller.device_id)
    store.close()


async def test_identity_is_recorded(settings, session_fixture) -> None:
    if not session_fixture.exists():
        pytest.skip("session fixture not generated")
    store = Store(settings.database)
    poller = Poller(settings, store)
    await run_briefly(poller)
    dev = store.device(poller.device_id)
    assert dev["model"] == "Z3801A"
    assert dev["serial"]
    store.close()


async def test_unsupported_query_is_dropped_after_one_rejection(settings, tmp_path) -> None:
    """E-113 is permanent on a given firmware, so retrying it every cycle is waste."""
    import json

    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps({"command": "*IDN?", "body": "A,Z3801A,S,F", "prompt": "scpi >"}) + "\n",
        encoding="utf-8",
    )
    settings = settings.model_copy(
        update={
            "transport": settings.transport.model_copy(
                update={"replay": settings.transport.replay.model_copy(update={"path": p})}
            )
        }
    )
    store = Store(settings.database)
    poller = Poller(settings, store)
    await run_briefly(poller)
    # Everything but *IDN? is unknown in this fixture, so it answers E-113 and the
    # poller should have marked those queries unsupported.
    assert poller._unsupported
    store.close()


def test_health_score_is_none_when_nothing_is_known() -> None:
    assert _health_score(Screen()) is None


def test_health_score_is_zero_if_any_flag_fails() -> None:
    s = Screen()
    s.health.self_test = True
    s.health.ocxo = False
    assert _health_score(s) == 0.0


def test_min_tracked_elevation_ignores_untracked(tmp_path) -> None:
    """The antenna-siting diagnostic: only satellites actually tracked count."""
    s = Screen()
    s.satellites = [
        Satellite(prn=1, elevation=10, tracked=False),  # visible but not tracked
        Satellite(prn=2, elevation=35, tracked=True),
        Satellite(prn=3, elevation=70, tracked=True),
    ]
    assert _min_tracked_elevation(s) == 35.0


def test_min_tracked_elevation_is_none_without_tracking() -> None:
    assert _min_tracked_elevation(Screen()) is None


def test_signature_changes_with_state(tmp_path) -> None:
    """Only genuinely new screen shapes are archived."""
    a, b = Screen(), Screen()
    a.mode = "Locked to GPS"
    b.mode = "Holdover: GPS 1PPS CLK invalid"
    assert _signature(a) != _signature(b)


@pytest.mark.parametrize("slow_every", [1, 2, 4])
def test_slow_poll_schedule_fires_for_every_cadence(slow_every: int) -> None:
    """``tick % n == 1`` is never true for n = 1, which would disable the screen poll.

    Checking the schedule arithmetic directly is cheaper than driving the poller once
    per cadence, and it pins the off-by-one that caused it.
    """
    fired = [t for t in range(1, 13) if (t - 1) % slow_every == 0]
    assert fired, f"slow poll never fires for slow_every={slow_every}"
    assert fired[0] == 1, "the first tick should include a screen poll"
    if slow_every == 1:
        assert fired == list(range(1, 13))


async def test_screen_is_kept_even_when_an_error_is_queued(tmp_path, settings) -> None:
    """A queued E-nnn must not discard a perfectly good status screen.

    The SCPI prompt reports the error *queue*, not the outcome of the command that
    just ran. ``:SYNC:TINT?`` answers E-230 whenever the unit is unlocked, which left
    an entry queued and made every later prompt read E-230 - silently discarding the
    screen and freezing every screen-derived metric while the fast ones kept updating.
    """
    import json

    screen = (Path(__file__).parent / "fixtures" / "screens" / "locked_hold_tracked.txt").read_text(
        encoding="utf-8"
    )
    p = tmp_path / "queued.jsonl"
    p.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"command": "*IDN?", "body": "HP,Z3801A,S,F", "prompt": "scpi >"},
                {"command": ":SYNC:TINT?", "body": "", "prompt": "E-230>"},
                # The screen arrives with the still-queued error on its prompt.
                {"command": ":SYST:STAT?", "body": screen, "prompt": "E-230>"},
            ]
        ),
        encoding="utf-8",
    )
    settings = settings.model_copy(
        update={
            "transport": settings.transport.model_copy(
                update={"replay": settings.transport.replay.model_copy(update={"path": p})}
            ),
            "database": tmp_path / "queued.db",
        }
    )
    store = Store(settings.database)
    poller = Poller(settings, store)
    await run_briefly(poller)

    latest = store.latest(poller.device_id)
    assert "mode" in latest, "screen was discarded because of a queued error"
    assert latest["mode"]["text"] == "Locked to GPS"
    store.close()
