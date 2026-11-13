"""Storage: writes, tier selection, state runs."""

from __future__ import annotations

import time

import pytest

from tautrack.device.screen import Satellite
from tautrack.store.db import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def device(store):
    return store.upsert_device(
        {"manufacturer": "HP", "model": "Z3801A", "serial": "ABC123", "firmware": "3543-A"}
    )


def test_device_upsert_is_idempotent_on_serial(store) -> None:
    """A reconnect must update the existing device, not create a duplicate."""
    a = store.upsert_device({"model": "Z3801A", "serial": "S1", "firmware": "1"})
    b = store.upsert_device({"model": "Z3801A", "serial": "S1", "firmware": "2"})
    assert a == b
    assert store.device(a)["firmware"] == "2"


def test_latest_returns_most_recent_value_per_metric(store, device) -> None:
    now = int(time.time())
    store.write_samples(device, now - 60, {"efc_percent": 54.0})
    store.write_samples(device, now, {"efc_percent": 55.0}, {"mode": "Locked to GPS"})
    latest = store.latest(device)
    assert latest["efc_percent"]["value"] == 55.0
    assert latest["mode"]["text"] == "Locked to GPS"


def test_none_values_are_not_stored(store, device) -> None:
    """A metric with no reading must be absent, not stored as a zero or a null row.

    1PPS TI is genuinely unavailable while unlocked; writing it as 0 would put a false
    reading in the middle of the accuracy chart.
    """
    now = int(time.time())
    store.write_samples(device, now, {"pps_ti_ns": None, "efc_percent": 54.0})
    assert "pps_ti_ns" not in store.latest(device)


def test_repeated_write_of_same_timestamp_is_idempotent(store, device) -> None:
    """A reconnect can retry a poll; that must not raise a constraint violation."""
    now = int(time.time())
    store.write_samples(device, now, {"tfom": 3.0})
    store.write_samples(device, now, {"tfom": 4.0})
    assert store.latest(device)["tfom"]["value"] == 4.0


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (300, "raw"),
        (86400, "raw"),
        (86401, "hour"),
        (60 * 86400, "hour"),
        (60 * 86400 + 1, "day"),
        (31536000, "day"),
    ],
)
def test_tier_selection_bounds_the_point_count(store, seconds, expected) -> None:
    """Spans beyond a day must come from aggregates, so a chart never draws millions."""
    assert store.choose_span(seconds) == expected


def test_series_reports_which_tier_answered(store, device) -> None:
    now = int(time.time())
    store.write_samples(device, now - 10, {"efc_percent": 54.0})
    s = store.series(device, "efc_percent", 3600, now=now)
    assert s.span == "raw"
    assert s.value == [54.0]


def test_state_spans_collapse_contiguous_runs(store, device) -> None:
    """Bands need runs, not points; collapsing here keeps long windows small."""
    now = int(time.time())
    for i, mode in enumerate(["Locked", "Locked", "Holdover", "Locked"]):
        store.write_samples(device, now - 300 + i * 60, {}, {"mode": mode})
    spans = store.state_spans(device, "mode", 3600, now=now)
    assert [s["text"] for s in spans] == ["Locked", "Holdover", "Locked"]
    assert spans[0]["end"] > spans[0]["start"]


def test_satellites_round_trip(store, device) -> None:
    now = int(time.time())
    store.write_satellites(
        device,
        now,
        [
            Satellite(prn=4, elevation=73, azimuth=120, signal=41, tracked=True),
            Satellite(prn=9, elevation=30, azimuth=200, tracked=False, acquiring=True),
        ],
    )
    sats = store.latest_satellites(device)
    assert len(sats) == 2
    tracked = next(s for s in sats if s["tracked"])
    assert tracked["prn"] == 4
    assert tracked["signal"] == 41
    untracked = next(s for s in sats if not s["tracked"])
    assert untracked["signal"] is None
    assert untracked["acquiring"] == 1


def test_screens_are_kept_only_once_per_signature(store, device) -> None:
    """Storing every screen would be ~1,400 near-identical rows a day."""
    now = int(time.time())
    store.write_screen(device, now, "sig-a", "screen one")
    store.write_screen(device, now + 60, "sig-a", "screen one again")
    store.write_screen(device, now + 120, "sig-b", "different state")
    rows = store.connection.execute("SELECT COUNT(*) AS n FROM screens").fetchone()["n"]
    assert rows == 2


def test_events_are_returned_newest_first(store) -> None:
    store.log_event("info", "poller", "first")
    store.log_event("error", "transport", "second")
    events = store.recent_events(10)
    assert events[0]["message"] == "second"
    assert events[0]["level"] == "error"


def test_concurrent_reads_during_writes_return_data(tmp_path) -> None:
    """Reads must not come back empty while another thread is writing.

    One connection is shared by the poller and the web threadpool. With
    ``check_same_thread=False`` and no lock, cursor state interleaves and reads that
    overlap a write return empty result sets - which showed up as charts rendering
    "no data yet" while their tiles had values, a different subset failing each time.
    """
    import threading

    store = Store(tmp_path / "race.db")
    device = store.upsert_device({"model": "Z3801A", "serial": "R1", "firmware": "f"})
    now = int(time.time())
    for i in range(200):
        store.write_samples(device, now - 200 + i, {"efc_percent": 54.0 + i * 0.001})

    empty: list[str] = []
    stop = threading.Event()

    def writer() -> None:
        t = now
        while not stop.is_set():
            t += 1
            store.write_samples(device, t, {"efc_percent": 55.0})

    def reader() -> None:
        for _ in range(60):
            s = store.series(device, "efc_percent", 3600, now=now)
            if not s.ts:
                empty.append("empty read")

    w = threading.Thread(target=writer, daemon=True)
    w.start()
    readers = [threading.Thread(target=reader) for _ in range(6)]
    for r in readers:
        r.start()
    for r in readers:
        r.join()
    stop.set()
    w.join(timeout=2)

    assert not empty, f"{len(empty)} reads returned no rows while a writer was active"
    store.close()
