"""The poll loop: fast discrete queries, slow status screen, reconnect with backoff.

Cadence follows C-1 and the measured timings. A fast tick issues ten short queries at
~0.2 s each; every Nth tick additionally fetches the status screen at ~2.84 s. The two
never overlap, because a single SCPI session serialises everything anyway (C-2).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from .config import Settings
from .device.queries import FAST_QUERIES, STATUS_QUERY
from .device.screen import Screen, parse_screen
from .device.session import ERR_DATA_STALE, ERR_UNDEFINED_HEADER, Session
from .store.db import Store
from .store.rollup import housekeep
from .transport import TransportError, build_transport

log = logging.getLogger(__name__)


class Poller:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.device_id: int | None = None
        self.last_error: str | None = None
        self.last_poll_ts: int | None = None
        self.connected = False
        #: Queries that answered E-113 are dropped for the process lifetime: the header
        #: does not exist on this firmware, so retrying wastes a slot every cycle.
        self._unsupported: set[str] = set()
        self._tick = 0

    async def run(self, stop: asyncio.Event) -> None:
        """Connect, poll, and reconnect with bounded backoff until asked to stop."""
        backoff = self.settings.transport.reconnect_min
        while not stop.is_set():
            session: Session | None = None
            try:
                transport = build_transport(self.settings.transport)
                session = Session(transport, read_timeout=self.settings.transport.read_timeout)
                await session.open()
                ident = await session.identify()
                self.device_id = self.store.upsert_device(ident)
                self.connected = True
                backoff = self.settings.transport.reconnect_min
                self.store.log_event(
                    "info", "transport", f"connected to {session.description}", str(ident)
                )
                log.info("connected to %s as %s", session.description, ident.get("model"))

                await self._poll_forever(session, stop)

            except TransportError as exc:
                self.connected = False
                self.last_error = str(exc)
                self.store.log_event("error", "transport", "transport failed", str(exc))
                log.warning("transport failed: %s; retrying in %.0fs", exc, backoff)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.store.log_event("error", "poller", "unexpected poller error", repr(exc))
                log.exception("unexpected poller error")
            finally:
                if session is not None:
                    with contextlib.suppress(Exception):
                        await session.close()

            if stop.is_set():
                break
            # Record the outage as a gap rather than interpolating across it (FR-6).
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, self.settings.transport.reconnect_max)

    async def _poll_forever(self, session: Session, stop: asyncio.Event) -> None:
        while not stop.is_set():
            started = time.monotonic()
            self._tick += 1
            await self._poll_once(session)
            # Hold the cadence regardless of how long the poll took, and never sleep
            # a negative interval if a slow screen overran the budget.
            elapsed = time.monotonic() - started
            delay = max(self.settings.poll.fast_seconds - elapsed, 0.5)
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)

    async def _poll_once(self, session: Session) -> None:
        assert self.device_id is not None
        ts = int(time.time())
        values: dict[str, float | None] = {}
        texts: dict[str, str] = {}

        await session.clear()

        for q in FAST_QUERIES:
            if q.command in self._unsupported:
                continue
            try:
                reply = await session.query(q.command)
            except TransportError:
                raise  # let run() reconnect
            if not reply.ok:
                code = reply.error_code
                if code == ERR_UNDEFINED_HEADER:
                    self._unsupported.add(q.command)
                    self.store.log_event(
                        "warning",
                        "poller",
                        f"{q.command} unsupported on this firmware (E-113); dropping",
                    )
                elif code == ERR_DATA_STALE and not q.stale_is_normal:
                    self.store.log_event(
                        "warning", "poller", f"{q.command} returned stale data (E-230)"
                    )
                # The prompt reports the state of the error QUEUE, not of this command.
                # Leaving an entry queued makes every later reply's prompt read E-nnn,
                # which previously caused good status screens to be discarded. Drain it.
                await session.query("*CLS")
                continue
            if q.text_parse is not None:
                if (text := q.text_parse(reply.body)) is not None:
                    texts[q.metric] = text
            elif q.parse is not None:
                values[q.metric] = q.parse(reply.body)

        screen: Screen | None = None
        # (tick - 1) % n, not tick % n == 1: with slow_every = 1 the latter is never
        # true, so "fetch the screen every tick" would silently fetch it never.
        if (self._tick - 1) % self.settings.poll.slow_every == 0:
            screen = await self._poll_screen(session, ts)

        if screen is not None:
            values.update(_screen_values(screen))
            texts.update(_screen_texts(screen))

        self.store.write_samples(self.device_id, ts, values, texts)
        self.last_poll_ts = ts

    async def _poll_screen(self, session: Session, ts: int) -> Screen | None:
        assert self.device_id is not None
        reply = await session.query(STATUS_QUERY, timeout=self.settings.transport.read_timeout)
        if not reply.body:
            self.store.log_event(
                "warning", "poller", "status screen query returned nothing", reply.prompt
            )
            return None
        if not reply.ok:
            # A queued error does not invalidate the screen that came with it: the body
            # is the data, and the prompt only says something is in the error queue.
            # Discarding on prompt alone silently stopped screen-derived metrics.
            self.store.log_event(
                "info", "poller", "status screen returned with a queued error", reply.prompt
            )
        screen = parse_screen(reply.body)
        if screen.parse_failures:
            # Surface as an event rather than writing nulls silently (FR-22). Two
            # prototype bugs were only ever caught because a value looked wrong by eye.
            self.store.log_event(
                "warning",
                "parser",
                f"{len(screen.parse_failures)} field(s) failed to parse",
                ",".join(screen.parse_failures),
            )
        self.store.write_satellites(self.device_id, ts, screen.satellites)
        self.store.write_screen(self.device_id, ts, _signature(screen), reply.body)
        return screen


def _signature(s: Screen) -> str:
    """State fingerprint, so only genuinely new screen shapes are archived."""
    return "|".join(
        str(x)
        for x in (
            s.mode,
            s.sync_state,
            s.position_mode,
            s.survey_blocked,
            s.position_kind,
            bool(s.satellites and any(x.tracked for x in s.satellites)),
            s.time_unconfirmed,
        )
    )


def _screen_values(s: Screen) -> dict[str, float | None]:
    return {
        "pps_ti_ns": s.pps_ti_ns,
        "tfom": float(s.tfom) if s.tfom is not None else None,
        "ffom": float(s.ffom) if s.ffom is not None else None,
        "satellites_tracked_screen": (
            float(s.tracking_count) if s.tracking_count is not None else None
        ),
        "satellites_visible": (
            float((s.tracking_count or 0) + (s.not_tracking_count or 0))
            if s.tracking_count is not None
            else None
        ),
        "holdover_predict_us_24h": s.holdover_predict_us_24h,
        "holdover_present_us": s.holdover_present_us,
        "holdover_duration_screen_s": (
            float(s.holdover_duration_s) if s.holdover_duration_s is not None else None
        ),
        "survey_percent": s.survey_percent,
        "height_m": s.height_m,
        "antenna_delay_ns": (float(s.antenna_delay_ns) if s.antenna_delay_ns is not None else None),
        "elevation_mask_deg": (
            float(s.elevation_mask_deg) if s.elevation_mask_deg is not None else None
        ),
        "health_ok": _health_score(s),
        # The lowest elevation actually tracked: the antenna-siting diagnostic that
        # proved more informative than signal strength during field testing.
        "min_tracked_elevation": _min_tracked_elevation(s),
    }


def _screen_texts(s: Screen) -> dict[str, str]:
    out: dict[str, str] = {}
    if s.mode:
        out["mode"] = s.mode
    if s.sync_state:
        out["sync_banner"] = s.sync_state
    if s.position_mode:
        out["position_mode"] = s.position_mode
    if s.survey_blocked:
        out["survey_blocked"] = s.survey_blocked
    if s.gps_time:
        out["gps_time"] = s.gps_time + (" [?]" if s.time_unconfirmed else "")
    if s.latitude:
        out["latitude"] = s.latitude
    if s.longitude:
        out["longitude"] = s.longitude
    if s.pps_clk_status:
        out["pps_clk_status"] = s.pps_clk_status
    return out


def _health_score(s: Screen) -> float | None:
    flags = [
        s.health.self_test,
        s.health.int_pwr,
        s.health.oven_pwr,
        s.health.ocxo,
        s.health.efc,
        s.health.gps_rcv,
    ]
    known = [f for f in flags if f is not None]
    if not known:
        return None
    return 1.0 if all(known) else 0.0


def _min_tracked_elevation(s: Screen) -> float | None:
    els = [x.elevation for x in s.satellites if x.tracked and x.elevation is not None]
    return float(min(els)) if els else None


async def housekeeping_loop(settings: Settings, store: Store, stop: asyncio.Event) -> None:
    """Periodically roll raw samples into trends and prune (Q3)."""
    interval = settings.retention.housekeeping_minutes * 60
    while not stop.is_set():
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        if stop.is_set():
            break
        try:
            stats = await asyncio.to_thread(
                housekeep,
                store._conn,
                settings.retention.raw_days,
                settings.retention.hourly_days,
                settings.retention.daily_days,
            )
            log.info("housekeeping: %s", stats)
        except Exception as exc:
            store.log_event("error", "rollup", "housekeeping failed", repr(exc))
            log.exception("housekeeping failed")
