"""Read-only web UI and JSON API (Q5).

Server-rendered with Jinja; charts are hydrated client-side from these endpoints. The
server picks the storage tier for a requested span (Q3), so the client asks for "this
metric over this long" and never needs to know whether it got raw samples or an
aggregate - though the response says which, so the UI can label it honestly.

No route mutates anything. There is no authentication in v1 (NFR-6), so the listener
defaults to loopback and exposure is a deliberate configuration choice.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__
from ..config import Settings
from ..store.db import Store

HERE = Path(__file__).parent

#: Selectable windows (FR-14), shortest first.
SPANS: dict[str, int] = {
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
    "3d": 259200,
    "1w": 604800,
    "2w": 1209600,
    "1mo": 2592000,
    "6mo": 15552000,
    "1y": 31536000,
}

#: Charted metrics, in display order, with presentation hints.
CHARTS: list[dict[str, Any]] = [
    {"metric": "pps_ti_ns", "title": "1PPS offset vs GPS", "unit": "ns", "zero": True},
    {"metric": "efc_percent", "title": "EFC steering", "unit": "%"},
    {"metric": "efc_dac", "title": "EFC (raw DAC)", "unit": "counts"},
    {"metric": "satellites_tracked", "title": "Satellites tracked", "unit": ""},
    {"metric": "min_tracked_elevation", "title": "Lowest elevation tracked", "unit": "deg"},
    {"metric": "ffom", "title": "FFOM (0 = settled)", "unit": ""},
    {"metric": "tfom", "title": "TFOM", "unit": ""},
    {"metric": "holdover_predict_us_24h", "title": "Holdover prediction", "unit": "us/24h"},
]


#: Plain-language help for every metric shown. Single source of truth: served as JSON
#: and rendered into the page, so the client-built tiles and the chart captions share
#: one wording.
#:
#: Written for someone who knows what a GPSDO is for but not what HP named things in
#: 1996. The figures of merit are opaque without explanation, and several of these
#: numbers invite misreading - EFC looks like a health percentage but is a position
#: within a tuning range, and the 1PPS offset changes units below a nanosecond.
METRIC_HELP: dict[str, dict[str, str]] = {
    "mode": {
        "title": "SmartClock mode",
        "text": (
            "What the oscillator control loop is doing. Power-up while acquiring "
            "satellites; Locked to GPS once it is steering the oscillator against GPS; "
            "Recovery while re-settling after an interruption; Holdover when usable GPS "
            "has been lost and it is coasting on the oven oscillator alone. Holdover is "
            "not a fault - the outputs stay usable, they just drift slowly."
        ),
    },
    "ffom": {
        "title": "FFOM - Frequency Figure of Merit",
        "text": (
            "The unit's own estimate of how accurate its FREQUENCY is, 0 to 3. Lower is "
            "better; 0 means the control loop has fully settled. It rises when the loop "
            "is disturbed - after a holdover, or while re-locking - and falls back as it "
            "re-converges. This is the number to check when asking whether the 10 MHz "
            "output can be trusted right now."
        ),
    },
    "tfom": {
        "title": "TFOM - Time Figure of Merit",
        "text": (
            "The same idea for TIME rather than frequency, 0 to 9, lower being better. "
            "It reflects confidence in the 1PPS edge. On this receiver it rests at 3 "
            "once locked and climbs to 9 during a cold start. Kept separate from FFOM "
            "because an oscillator can be exactly on frequency while its pulse edge is "
            "still offset."
        ),
    },
    "pps_ti_ns": {
        "title": "1PPS offset vs GPS",
        "text": (
            "How far the unit's one-pulse-per-second output sits from GPS time, in "
            "nanoseconds - the headline accuracy figure. Signed: positive is ahead of "
            "GPS, negative behind. The device switches to PICOSECONDS once the offset "
            "drops below a nanosecond; that is converted here so the scale stays "
            "consistent. Unavailable while unlocked, as there is nothing to compare to."
        ),
    },
    "efc_percent": {
        "title": "EFC - Electronic Frequency Control",
        "text": (
            "Where the control voltage sits within the oven oscillator's tuning range, "
            "as a percentage. NOT a health score - mid-range is ideal, because it leaves "
            "room to steer in both directions. The long-term trend is what matters: "
            "crystals age, so this creeps one way over years, and once it reaches an "
            "extreme the unit can no longer discipline the oscillator at all. That makes "
            "it the best early warning of end-of-life."
        ),
    },
    "efc_dac": {
        "title": "EFC raw DAC count",
        "text": (
            "The same control value as an integer converter count rather than a rounded "
            "percentage. Finer resolution and no floating-point rounding, which makes it "
            "the better series for spotting slow aging across months and years."
        ),
    },
    "satellites_tracked": {
        "title": "Satellites tracked",
        "text": (
            "How many satellites the receiver is actually using, against how many it can "
            "see. Two thresholds matter: FOUR are needed to survey a position, but only "
            "ONE is needed to keep time once the position is known and held. A unit in "
            "Hold can therefore stay locked on a single satellite indefinitely."
        ),
    },
    "min_tracked_elevation": {
        "title": "Lowest elevation tracked",
        "text": (
            "The angle above the horizon of the lowest satellite currently tracked. This "
            "is the most useful antenna-siting diagnostic there is, and more telling than "
            "signal strength: an obstructed antenna tracks only satellites high overhead, "
            "so a value that stays high means a blocked horizon even when the signals it "
            "does receive look strong."
        ),
    },
    "holdover_predict_us_24h": {
        "title": "Holdover prediction",
        "text": (
            "The unit's estimate of how far its time would drift over 24 hours if GPS "
            "vanished, in microseconds. It improves as SmartClock learns this particular "
            "oscillator, so a falling value means the unit is getting to know its own "
            "crystal - the best single indicator of how far it can be trusted without GPS."
        ),
    },
    "position_mode": {
        "title": "Position mode",
        "text": (
            "Survey means the receiver is still averaging its own position, which needs "
            "four satellites at once and can take hours. Hold means the position is fixed "
            "and it no longer solves for location, only for time - which is why one "
            "satellite then suffices."
        ),
    },
    "health_ok": {
        "title": "Health monitor",
        "text": (
            "A roll-up of six internal self-tests: self test, internal power, oven power, "
            "the OCXO itself, EFC range, and the GPS receiver. Any one failing shows here "
            "as FAULT."
        ),
    },
    "sky": {
        "title": "Sky view",
        "text": (
            "Satellite positions as seen from the antenna. The centre is straight "
            "overhead, the outer rim is the horizon; north up, east right. Filled markers "
            "are tracked, hollow ones visible but unused, and colour indicates signal "
            "strength. The dashed orange ring is the elevation mask - anything below it is "
            "ignored by the receiver."
        ),
    },
    "clocks": {
        "title": "Clocks",
        "text": (
            "All three describe the UNIT, not this computer. The first is exactly what the "
            "device reports; 1996 firmware has a GPS week-number rollover bug that places "
            "its DATE 1024 weeks in the past while its time-of-day stays correct. The "
            "other two apply that offset for display only - stored data keeps what the "
            "device actually said. This machine's own clock is shown separately so a "
            "timezone difference is not mistaken for instrument drift."
        ),
    },
}


def _tz(name: str) -> tzinfo:
    """Resolve a timezone name, degrading safely rather than raising.

    Neither Windows nor python:*-slim ships the IANA database, so ZoneInfo fails for
    every key including "UTC" unless the `tzdata` package is installed - which is why
    it is a hard dependency. This still falls back to the fixed-offset UTC from the
    standard library, because a clock display must never be able to 500 the whole
    status endpoint.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return UTC


def create_app(settings: Settings, store: Store, poller: Any = None) -> FastAPI:
    app = FastAPI(title="tautrack", version=__version__, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    def device_id() -> int:
        dev = store.any_device()
        if dev is None:
            raise HTTPException(status_code=503, detail="no device has reported yet")
        return int(dev["id"])

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        """Liveness only - the process is up and the database is readable.

        Deliberately not tied to device connectivity: a GPSDO in holdover, or an
        unplugged antenna, must not make the container look unhealthy and get
        restarted. Device state is reported through /api/status instead.
        """
        try:
            store.any_device()
        except Exception as exc:
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)
        return JSONResponse({"status": "ok", "version": __version__})

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        dev = store.any_device()
        if dev is None:
            return {
                "device": None,
                "connected": bool(getattr(poller, "connected", False)),
                "last_error": getattr(poller, "last_error", None),
                "latest": {},
                "clocks": _clocks(settings, None, live=settings.transport.kind != "replay"),
            }
        latest = store.latest(int(dev["id"]))
        unit_time = (latest.get("gps_time") or {}).get("text")
        return {
            "device": {
                "model": dev["model"],
                "serial": dev["serial"],
                "firmware": dev["firmware"],
            },
            "connected": bool(getattr(poller, "connected", False)),
            "last_error": getattr(poller, "last_error", None),
            "last_poll": getattr(poller, "last_poll_ts", None),
            "latest": latest,
            "transport": settings.transport.kind,
            "clocks": _clocks(settings, unit_time, live=settings.transport.kind != "replay"),
            "version": __version__,
        }

    @app.get("/api/series")
    def api_series(
        metric: str = Query(...),
        span: str = Query("1h"),
    ) -> dict[str, Any]:
        if span not in SPANS:
            raise HTTPException(status_code=400, detail=f"unknown span {span!r}")
        s = store.series(
            device_id(),
            metric,
            SPANS[span],
            # Fallback only: used when there are too few points to measure the
            # cadence from the data itself.
            expected_interval=settings.poll.fast_seconds,
        )
        return {
            "metric": s.metric,
            # Which tier answered, so the UI can say "hourly averages" rather than
            # implying raw fidelity across a year.
            "resolution": s.span,
            "ts": s.ts,
            "value": s.value,
            "min": s.min_value,
            "max": s.max_value,
            # Ranges with no data, so the chart can show absence rather than
            # interpolating a straight line across an outage.
            "gaps": s.gaps,
        }

    @app.get("/api/states")
    def api_states(span: str = Query("1h"), metric: str = Query("mode")) -> dict[str, Any]:
        """Contiguous runs of an enum metric, for shaded bands behind the charts."""
        if span not in SPANS:
            raise HTTPException(status_code=400, detail=f"unknown span {span!r}")
        return {"metric": metric, "spans": store.state_spans(device_id(), metric, SPANS[span])}

    @app.get("/api/satellites")
    def api_satellites(trail: str | None = Query(None)) -> dict[str, Any]:
        did = device_id()
        current = store.latest_satellites(did)
        history: list[dict[str, Any]] = []
        if trail in SPANS:
            history = store.satellite_history(did, SPANS[trail])
        latest = store.latest(did)
        mask = (latest.get("elevation_mask_deg") or {}).get("value")
        return {
            "current": current,
            "history": history,
            "elevation_mask": mask,
        }

    @app.get("/api/help")
    def api_help() -> dict[str, Any]:
        """Metric descriptions, so the UI and any other client share one wording."""
        return METRIC_HELP

    @app.get("/api/events")
    def api_events(limit: int = Query(30, ge=1, le=200)) -> dict[str, Any]:
        return {"events": store.recent_events(limit)}

    @app.get("/")
    def index(request: Request):
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "spans": list(SPANS),
                "charts": CHARTS,
                "metric_help": json.dumps(METRIC_HELP),
                "version": __version__,
                "timezone": settings.display.timezone,
            },
        )

    return app


def _clocks(settings: Settings, unit_time: str | None, live: bool = True) -> dict[str, Any]:
    """Clocks (FR-18), all three describing the **unit**, with the host shown apart.

    An earlier version showed the unit's corrected time next to the *host's* local
    clock, which invited reading them as one instant in two zones. They are not: with a
    UTC-6 timezone that looks like six hours of drift even when nothing is wrong. All
    three unit clocks now derive from the same reported instant, so UTC and local
    differ by exactly the zone offset, and any real disagreement with this machine is
    reported once, explicitly, as ``host_delta_seconds``.

    The device's time-of-day is UTC; only its *date* is wrong, by 1024 GPS weeks on
    1996 firmware. The offset is applied for display only - stored samples keep exactly
    what was reported.

    Formats are normalised to ``YYYY-MM-DD HH:MM:SS`` throughout, including the
    device's own ``HH:MM:SS DD Mon YYYY``, so the three can be compared by eye.
    """
    now = datetime.now(UTC)
    tz = _tz(settings.display.timezone)
    offset = timedelta(days=settings.display.gps_rollover_days)
    zone_label = now.astimezone(tz).strftime("%Z") or settings.display.timezone

    reported_iso: str | None = None
    corrected_utc: str | None = None
    corrected_local: str | None = None
    delta: float | None = None
    unconfirmed = bool(unit_time and "[?]" in unit_time)

    if unit_time:
        raw = unit_time.replace("[?]", "").strip()
        for fmt in ("%H:%M:%S %d %b %Y", "%H:%M:%S %d %B %Y"):
            try:
                parsed = datetime.strptime(raw, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
            reported_iso = parsed.strftime("%Y-%m-%d %H:%M:%S")
            fixed = parsed + offset
            corrected_utc = fixed.strftime("%Y-%m-%d %H:%M:%S")
            corrected_local = fixed.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")
            delta = (now - fixed).total_seconds()
            break

    return {
        "unit_reported_raw": unit_time,
        "unit_reported": reported_iso,
        "unit_corrected_utc": corrected_utc,
        "unit_corrected_local": corrected_local,
        "unit_unconfirmed": unconfirmed,
        "host_utc": now.strftime("%Y-%m-%d %H:%M:%S"),
        "host_local": now.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
        # Positive means the unit's reported time lags this machine. Under replay this
        # is simply the age of the recorded screen, not instrument error - so the UI
        # must say which transport produced it.
        "host_delta_seconds": None if delta is None else round(delta, 1),
        "live": live,
        "timezone": settings.display.timezone,
        "zone_label": zone_label,
        "rollover_days": settings.display.gps_rollover_days,
        "epoch": int(time.time()),
    }
