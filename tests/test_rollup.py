"""Rollups: the Zabbix-style history/trends tiering (Q3)."""

from __future__ import annotations

import time

import pytest

from tautrack.store.db import Store
from tautrack.store.rollup import DAY, HOUR, housekeep, prune, rollup_scalars


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture
def device(store):
    return store.upsert_device({"model": "Z3801A", "serial": "S1", "firmware": "f"})


def test_aggregate_preserves_min_and_max_not_just_mean(store, device) -> None:
    """FR-10, and the whole reason trends keep three numbers.

    A brief excursion - a ten-minute holdover, an EFC spike - disappears from an
    average but must stay visible in a one-year view.
    """
    base = (int(time.time()) // HOUR) * HOUR - 2 * HOUR
    for i in range(10):
        value = 54.0 if i != 5 else 61.0  # one excursion among ordinary readings
        store.write_samples(device, base + i * 60, {"efc_percent": value})

    with store.connection:
        rollup_scalars(store.connection, "hour")

    row = store.connection.execute(
        "SELECT count, min_value, avg_value, max_value FROM trends WHERE span='hour'"
    ).fetchone()
    assert row["count"] == 10
    assert row["max_value"] == pytest.approx(61.0)  # the excursion survives
    assert row["min_value"] == pytest.approx(54.0)
    assert row["avg_value"] < 55.0  # the mean alone would hide it


def test_current_partial_bucket_is_not_aggregated(store, device) -> None:
    """An open bucket would be written with an incomplete count and left stale."""
    now = int(time.time())
    store.write_samples(device, now, {"tfom": 3.0})
    with store.connection:
        rollup_scalars(store.connection, "hour", now=now)
    current = (now // HOUR) * HOUR
    row = store.connection.execute(
        "SELECT COUNT(*) AS n FROM trends WHERE span='hour' AND bucket = ?", (current,)
    ).fetchone()
    assert row["n"] == 0


def test_rollup_is_idempotent(store, device) -> None:
    """Housekeeping runs repeatedly; a second pass must not double-count."""
    base = (int(time.time()) // HOUR) * HOUR - 3 * HOUR
    for i in range(5):
        store.write_samples(device, base + i * 60, {"ffom": float(i)})
    with store.connection:
        rollup_scalars(store.connection, "hour")
        rollup_scalars(store.connection, "hour")
    row = store.connection.execute("SELECT count FROM trends WHERE span='hour'").fetchone()
    assert row["count"] == 5


def test_prune_removes_raw_beyond_retention(store, device) -> None:
    now = int(time.time())
    store.write_samples(device, now - 20 * DAY, {"efc_percent": 54.0})
    store.write_samples(device, now, {"efc_percent": 55.0})
    with store.connection:
        deleted = prune(store.connection, raw_days=14, hourly_days=400, daily_days=3650, now=now)
    assert deleted["samples"] == 1
    remaining = store.connection.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
    assert remaining == 1


def test_housekeeping_aggregates_before_pruning(store, device) -> None:
    """Ordering matters: pruning first would discard data that was never aggregated."""
    now = int(time.time())
    old = now - 20 * DAY
    for i in range(6):
        store.write_samples(device, old + i * 60, {"efc_percent": 54.0 + i})

    housekeep(store.connection, raw_days=14, hourly_days=400, daily_days=3650, now=now)

    # Raw rows are gone...
    assert store.connection.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"] == 0
    # ...but their aggregate survived, with the extremes intact.
    row = store.connection.execute(
        "SELECT count, min_value, max_value FROM trends WHERE span='hour'"
    ).fetchone()
    assert row is not None
    assert row["count"] == 6
    assert row["max_value"] == pytest.approx(59.0)


def test_satellite_trends_keep_appearance_count(store, device) -> None:
    """How often a PRN was visible is as diagnostic as how strong it was (Q9)."""
    from tautrack.device.screen import Satellite

    base = (int(time.time()) // HOUR) * HOUR - 2 * HOUR
    for i in range(4):
        store.write_satellites(
            device,
            base + i * 60,
            [Satellite(prn=3, elevation=50 + i, azimuth=100, signal=40 + i, tracked=i % 2 == 0)],
        )
    housekeep(store.connection, now=int(time.time()))
    row = store.connection.execute(
        "SELECT count, tracked_count, max_signal, min_elevation FROM sat_trends "
        "WHERE span='hour' AND prn=3"
    ).fetchone()
    assert row["count"] == 4  # appeared four times
    assert row["tracked_count"] == 2  # but was only tracked twice
    assert row["max_signal"] == 43
    assert row["min_elevation"] == 50
