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


def test_gap_is_reported_and_breaks_the_line(store, device) -> None:
    """A gap is an absence of ROWS, not a row containing null.

    Without explicit handling a chart joins the samples either side of an outage with a
    straight line, presenting an hour of missing data as though it had been measured.
    """
    now = int(time.time())
    # 10 samples a minute apart, a 30-minute hole, then 10 more.
    for i in range(10):
        store.write_samples(device, now - 3600 + i * 60, {"efc_percent": 54.0})
    for i in range(10):
        store.write_samples(device, now - 1200 + i * 60, {"efc_percent": 55.0})

    s = store.series(device, "efc_percent", 7200, now=now)

    assert len(s.gaps) == 1, f"expected one gap, got {s.gaps}"
    start, end = s.gaps[0]
    assert end - start > 1500, "the gap should span the missing half hour"
    # A null must be present so the renderer actually breaks the stroke.
    assert None in s.value
    # And the real samples must be untouched.
    assert [v for v in s.value if v is not None] == [54.0] * 10 + [55.0] * 10


def test_a_single_missed_poll_is_not_called_a_gap(store, device) -> None:
    """One dropped sample is normal; flagging it would cry wolf constantly."""
    now = int(time.time())
    for i in range(20):
        if i == 10:
            continue  # skip exactly one
        store.write_samples(device, now - 1200 + i * 60, {"tfom": 3.0})
    s = store.series(device, "tfom", 3600, now=now)
    assert s.gaps == [], f"a single missed poll was reported as a gap: {s.gaps}"


def test_gap_detection_adapts_to_the_data_cadence(store, device) -> None:
    """Cadence is measured, not assumed.

    Backfilled history can carry a different interval from the live poller, and
    trusting a configured value there turns every normal interval into a false gap.
    """
    now = int(time.time())
    for i in range(15):
        store.write_samples(device, now - 15 * 600 + i * 600, {"ffom": 1.0})  # 10 min apart
    # An expectation an order of magnitude too small must not create spurious gaps.
    s = store.series(device, "ffom", 24 * 3600, now=now, expected_interval=15.0)
    assert s.gaps == [], f"measured cadence ignored: {s.gaps}"


def test_mixed_cadences_in_one_window_are_not_all_gaps(store, device) -> None:
    """A window can contain two cadences at once.

    Backfilled history recorded every 66 s sits alongside live data polled every 15 s.
    Estimating the cadence with a median follows whichever is more numerous, so every
    interval of the slower one looks like an outage - which produced 726 false gaps
    across 24 hours of real data, most of them 1.1 minutes long.
    """
    now = int(time.time())
    t = now - 20000
    # Older, slower history...
    for _ in range(60):
        store.write_samples(device, t, {"efc_percent": 54.0})
        t += 66
    # ...then a faster live cadence, more numerous so it would dominate a median.
    for _ in range(300):
        store.write_samples(device, t, {"efc_percent": 54.5})
        t += 15

    s = store.series(device, "efc_percent", 24 * 3600, now=now)
    assert s.gaps == [], f"cadence change reported as {len(s.gaps)} gaps: {s.gaps[:3]}"


def test_a_real_outage_is_still_found_among_mixed_cadences(store, device) -> None:
    """Tolerating the slower cadence must not blind us to an actual outage."""
    now = int(time.time())
    t = now - 20000
    for _ in range(60):
        store.write_samples(device, t, {"tfom": 3.0})
        t += 66
    t += 1800  # a half-hour hole
    for _ in range(300):
        store.write_samples(device, t, {"tfom": 3.0})
        t += 15

    s = store.series(device, "tfom", 24 * 3600, now=now)
    assert len(s.gaps) == 1, f"expected exactly the real outage, got {s.gaps}"
    assert s.gaps[0][1] - s.gaps[0][0] > 1500
