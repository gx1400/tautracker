"""Read-only web UI and JSON API (Q5).

Server-rendered with Jinja; charts are hydrated client-side from these endpoints. The
server picks the storage tier for a requested span (Q3), so the client asks for "this
metric over this long" and never needs to know whether it got raw samples or an
aggregate - though the response says which, so the UI can label it honestly.

No route mutates anything. There is no authentication in v1 (NFR-6), so the listener
defaults to loopback and exposure is a deliberate configuration choice.
"""

from __future__ import annotations

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
                "clocks": _clocks(settings, None),
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
            "clocks": _clocks(settings, unit_time),
            "version": __version__,
        }

    @app.get("/api/series")
    def api_series(
        metric: str = Query(...),
        span: str = Query("1h"),
    ) -> dict[str, Any]:
        if span not in SPANS:
            raise HTTPException(status_code=400, detail=f"unknown span {span!r}")
        s = store.series(device_id(), metric, SPANS[span])
        return {
            "metric": s.metric,
            # Which tier answered, so the UI can say "hourly averages" rather than
            # implying raw fidelity across a year.
            "resolution": s.span,
            "ts": s.ts,
            "value": s.value,
            "min": s.min_value,
            "max": s.max_value,
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
                "version": __version__,
                "timezone": settings.display.timezone,
            },
        )

    return app


def _clocks(settings: Settings, unit_time: str | None) -> dict[str, Any]:
    """Three clocks (FR-18).

    The unit's reported time is shown verbatim, including any ``[?]``. Because 1996
    firmware reports a date 1024 GPS weeks behind actual, the corrected values add the
    configured offset back - a display correction only. Stored samples keep exactly
    what the device said.
    """
    now = datetime.now(UTC)
    tz = _tz(settings.display.timezone)
    offset = timedelta(days=settings.display.gps_rollover_days)

    corrected: str | None = None
    if unit_time:
        raw = unit_time.replace("[?]", "").strip()
        for fmt in ("%H:%M:%S %d %b %Y", "%H:%M:%S %d %B %Y"):
            try:
                parsed = datetime.strptime(raw, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
            corrected = (parsed + offset).isoformat()
            break

    return {
        "unit_reported": unit_time,
        "unit_corrected_utc": corrected,
        "host_utc": now.isoformat(),
        "host_local": now.astimezone(tz).isoformat(),
        "timezone": settings.display.timezone,
        "rollover_days": settings.display.gps_rollover_days,
        "epoch": int(time.time()),
    }
