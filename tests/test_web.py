"""Web API: read-only endpoints, tier labelling, and the clock correction."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tautrack.config import Settings
from tautrack.device.screen import Satellite
from tautrack.store.db import Store
from tautrack.web.app import create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        database=tmp_path / "t.db",
        display={"timezone": "America/Chicago", "gps_rollover_days": 7168},
    )
    store = Store(settings.database)
    device = store.upsert_device(
        {"manufacturer": "HP", "model": "Z3801A", "serial": "S1", "firmware": "3543-A"}
    )
    now = int(time.time())
    for i in range(10):
        store.write_samples(
            device,
            now - (10 - i) * 60,
            {"pps_ti_ns": -100.0 + i, "efc_percent": 54.0 + i * 0.001},
            {"mode": "Locked to GPS" if i < 7 else "Holdover: GPS 1PPS CLK invalid"},
        )
    store.write_samples(
        device, now, {"elevation_mask_deg": 10.0}, {"gps_time": "12:00:00 30 Mar 2007"}
    )
    store.write_satellites(
        device, now, [Satellite(prn=4, elevation=70, azimuth=120, signal=80, tracked=True)]
    )
    with TestClient(create_app(settings, store, poller=None)) as c:
        yield c
    store.close()


def test_healthz_reports_liveness_not_device_state(client) -> None:
    """A GPSDO in holdover must not make the container look unhealthy."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_renders(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "tautrack" in r.text
    assert "Sky view" in r.text


def test_status_exposes_device_and_latest(client) -> None:
    d = client.get("/api/status").json()
    assert d["device"]["model"] == "Z3801A"
    assert d["latest"]["pps_ti_ns"]["value"] == pytest.approx(-91.0)
    assert d["latest"]["mode"]["text"].startswith("Holdover")


def test_clock_applies_the_rollover_correction_for_display_only(client) -> None:
    """1996 firmware reports 1024 GPS weeks behind; the stored value stays untouched."""
    clocks = client.get("/api/status").json()["clocks"]
    # Reported time is normalised to the same format as the corrected values so the
    # three can be compared by eye.
    assert clocks["unit_reported"] == "2007-03-30 12:00:00"
    assert clocks["unit_reported_raw"] == "12:00:00 30 Mar 2007"
    assert clocks["unit_corrected_utc"].startswith("2026-")
    assert clocks["rollover_days"] == 7168
    # The raw sample is unchanged.
    d = client.get("/api/status").json()
    assert d["latest"]["gps_time"]["text"] == "12:00:00 30 Mar 2007"


def test_all_three_clocks_describe_the_unit(client) -> None:
    """UTC and local must differ by exactly the zone offset, nothing else.

    An earlier version put the host's wall clock in the third slot, so a UTC-6 zone
    looked like six hours of instrument drift.
    """
    from datetime import datetime

    c = client.get("/api/status").json()["clocks"]
    utc = datetime.fromisoformat(c["unit_corrected_utc"])
    local = datetime.fromisoformat(c["unit_corrected_local"])
    # America/Chicago is UTC-6 in November.
    assert (utc - local).total_seconds() == 6 * 3600


def test_unit_vs_host_delta_is_reported_once_and_explicitly(client) -> None:
    """Any real disagreement with this machine belongs in one labelled field."""
    c = client.get("/api/status").json()["clocks"]
    assert "host_delta_seconds" in c
    assert isinstance(c["host_delta_seconds"], (int, float))


def test_replay_transport_is_flagged_as_not_live(client) -> None:
    """Under replay the delta is the age of a recording, not instrument error."""
    c = client.get("/api/status").json()["clocks"]
    # The fixture uses the default transport, which is replay.
    assert c["live"] is False


def test_series_returns_points_and_labels_the_tier(client) -> None:
    d = client.get("/api/series?metric=pps_ti_ns&span=1h").json()
    assert d["resolution"] == "raw"
    assert len(d["ts"]) == 10


def test_long_span_is_served_from_aggregates(client) -> None:
    """A year of raw samples would be millions of points, so the tier must change."""
    d = client.get("/api/series?metric=pps_ti_ns&span=1y").json()
    assert d["resolution"] == "day"


def test_unknown_span_is_rejected(client) -> None:
    assert client.get("/api/series?metric=pps_ti_ns&span=7y").status_code == 400


def test_states_collapse_into_runs_for_shaded_bands(client) -> None:
    d = client.get("/api/states?span=1h&metric=mode").json()
    assert [s["text"] for s in d["spans"]] == [
        "Locked to GPS",
        "Holdover: GPS 1PPS CLK invalid",
    ]


def test_satellites_include_the_elevation_mask(client) -> None:
    d = client.get("/api/satellites").json()
    assert d["elevation_mask"] == 10.0
    assert d["current"][0]["prn"] == 4


def test_satellite_trails_are_opt_in(client) -> None:
    assert client.get("/api/satellites").json()["history"] == []
    assert isinstance(client.get("/api/satellites?trail=1h").json()["history"], list)


def test_api_is_read_only(client) -> None:
    """No route mutates; POST to any of them should not be routed."""
    for path in ("/api/status", "/api/series", "/api/satellites", "/"):
        assert client.post(path).status_code in (404, 405)
