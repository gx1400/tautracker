"""SQLite storage: writes, queries, and the raw-vs-trends tier selection.

Why tier selection lives here rather than in the UI: picking the right resolution is a
storage concern, and the web layer should ask for "this metric over this span" without
knowing whether the answer comes from raw samples or an aggregate (Q5).
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

#: Spans at or below this get raw samples; longer spans use aggregates. Chosen so a
#: chart never tries to draw more than a few thousand points: at a 15 s cadence, 24 h
#: is ~5,760 samples, which uPlot handles comfortably.
RAW_MAX_SECONDS = 24 * 3600
HOURLY_MAX_SECONDS = 60 * 24 * 3600


#: A gap is declared when consecutive samples are further apart than this multiple
#: of the expected cadence. Three leaves room for one missed poll without crying wolf,
#: while still catching a real outage.
GAP_FACTOR = 3.0


@dataclass(slots=True)
class Series:
    """A metric over time, with its resolution and its holes made explicit.

    ``span`` tells the caller which tier answered, so the UI can say so rather than
    silently implying raw fidelity over a one-year view.

    ``gaps`` matters because a gap here is an *absence of rows*, not a row containing
    null. Without it a chart joins the samples either side of an outage with a straight
    line, drawing an hour of missing data as though it had been measured.
    """

    metric: str
    span: str  # raw | hour | day
    ts: list[int]
    value: list[float | None]
    min_value: list[float | None] | None = None
    max_value: list[float | None] | None = None
    #: Inclusive (start, end) epoch pairs with no data between them.
    gaps: list[tuple[int, int]] = field(default_factory=list)


class _Result:
    """Rows already materialised under the lock.

    Returning a live cursor would let ``.fetchone()`` run outside the lock, which is
    the race this wrapper exists to prevent.
    """

    __slots__ = ("_rows", "rowcount")

    def __init__(self, rows: list[Any], rowcount: int = -1) -> None:
        self._rows = rows
        #: Rows affected by a DML statement; the rollup and prune both report on it.
        self.rowcount = rowcount

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Any]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _LockedConnection:
    """Serialises all access to one sqlite3.Connection.

    The connection is shared by the poller (writing) and the web threadpool (reading
    many series concurrently for the charts). ``check_same_thread=False`` silences
    sqlite3's guard but does **not** make concurrent ``execute()`` safe: cursor state
    interleaves, and reads overlapping a write come back empty.

    That surfaced as charts rendering "no data yet" while their own tiles showed
    values, with a *different* subset failing on each page load - the hallmark of a
    race rather than missing data.

    A single lock is ample here: at roughly four writes a minute against a handful of
    reads per page, contention is irrelevant next to correctness.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql: str, parameters: Any = ()) -> _Result:
        with self._lock:
            cur = self._conn.execute(sql, parameters)
            return _Result(cur.fetchall(), cur.rowcount)

    def executemany(self, sql: str, seq: Any) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # Transaction context manager, holding the lock for its whole duration so a
    # concurrent reader cannot observe a half-applied batch.
    def __enter__(self):
        self._lock.acquire()
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            self._conn.__exit__(*exc)
        finally:
            self._lock.release()

    @property
    def raw(self) -> sqlite3.Connection:
        """The underlying connection. Use only where the lock is already held."""
        return self._conn


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        raw.row_factory = sqlite3.Row
        self._conn = _LockedConnection(raw)
        self._metric_ids: dict[str, int] = {}
        self._init_schema()

    @property
    def connection(self) -> _LockedConnection:
        """The locked connection, for the rollup's multi-statement batch.

        Its ``with`` block holds the lock for the whole transaction, so housekeeping
        cannot interleave with a poll or with a chart query.
        """
        return self._conn

    def close(self) -> None:
        self._conn.close()

    def _init_schema(self) -> None:
        sql = resources.files("tautrack.store").joinpath("schema.sql").read_text(encoding="utf-8")
        with self._conn:
            self._conn.executescript(sql)

    # ---------------------------------------------------------------- devices

    def upsert_device(self, ident: dict[str, str]) -> int:
        now = int(time.time())
        serial = ident.get("serial") or "unknown"
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO devices (manufacturer, model, serial, firmware, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(serial) DO UPDATE SET
                    manufacturer = excluded.manufacturer,
                    model        = excluded.model,
                    firmware     = excluded.firmware,
                    last_seen    = excluded.last_seen
                """,
                (
                    ident.get("manufacturer"),
                    ident.get("model"),
                    serial,
                    ident.get("firmware"),
                    now,
                    now,
                ),
            )
        row = self._conn.execute("SELECT id FROM devices WHERE serial = ?", (serial,)).fetchone()
        return int(row["id"])

    def device(self, device_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return dict(row) if row else None

    def any_device(self) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM devices ORDER BY id LIMIT 1").fetchone()
        return dict(row) if row else None

    # ---------------------------------------------------------------- metrics

    def metric_id(self, name: str, unit: str | None = None, kind: str = "gauge") -> int:
        cached = self._metric_ids.get(name)
        if cached is not None:
            return cached
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO metrics (name, unit, kind) VALUES (?, ?, ?)",
                (name, unit, kind),
            )
        row = self._conn.execute("SELECT id FROM metrics WHERE name = ?", (name,)).fetchone()
        mid = int(row["id"])
        self._metric_ids[name] = mid
        return mid

    # ---------------------------------------------------------------- writes

    def write_samples(
        self,
        device_id: int,
        ts: int,
        values: dict[str, float | None],
        texts: dict[str, str] | None = None,
    ) -> None:
        """Insert a batch of scalar samples sharing one timestamp.

        ``INSERT OR REPLACE`` makes a retried poll idempotent rather than a constraint
        violation, which matters when a transport reconnects mid-cycle.
        """
        rows = []
        for name, value in values.items():
            if value is None:
                continue
            rows.append((device_id, self.metric_id(name), ts, float(value), None))
        for name, text in (texts or {}).items():
            if text is None:
                continue
            rows.append((device_id, self.metric_id(name, kind="state"), ts, None, text))
        if not rows:
            return
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO samples (device_id, metric_id, ts, value, text) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )

    def write_satellites(self, device_id: int, ts: int, sats: list[Any]) -> None:
        if not sats:
            return
        rows = [
            (
                device_id,
                ts,
                s.prn,
                s.elevation,
                s.azimuth,
                s.signal,
                1 if s.tracked else 0,
                1 if s.acquiring else 0,
            )
            for s in sats
        ]
        with self._conn:
            self._conn.executemany(
                "INSERT OR REPLACE INTO sat_samples "
                "(device_id, ts, prn, elevation, azimuth, signal, tracked, acquiring) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def write_screen(self, device_id: int, ts: int, signature: str, body: str) -> None:
        """Keep a screen only when its state fingerprint is new.

        Storing every screen would add ~1,400 rows a day of near-identical text; storing
        only new signatures keeps the forensic value at a fraction of the cost.
        """
        row = self._conn.execute(
            "SELECT 1 FROM screens WHERE device_id = ? AND signature = ? LIMIT 1",
            (device_id, signature),
        ).fetchone()
        if row:
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO screens (device_id, ts, signature, body) VALUES (?, ?, ?, ?)",
                (device_id, ts, signature, body),
            )

    def log_event(self, level: str, source: str, message: str, detail: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO events (ts, level, source, message, detail) VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), level, source, message, detail),
            )

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- reads

    def latest(self, device_id: int) -> dict[str, Any]:
        """Most recent value of every metric, scalar and text alike."""
        rows = self._conn.execute(
            """
            SELECT m.name, s.value, s.text, s.ts
            FROM samples s
            JOIN metrics m ON m.id = s.metric_id
            JOIN (
                SELECT metric_id, MAX(ts) AS ts
                FROM samples WHERE device_id = ?
                GROUP BY metric_id
            ) latest ON latest.metric_id = s.metric_id AND latest.ts = s.ts
            WHERE s.device_id = ?
            """,
            (device_id, device_id),
        ).fetchall()
        out: dict[str, Any] = {}
        for r in rows:
            out[r["name"]] = {"value": r["value"], "text": r["text"], "ts": r["ts"]}
        return out

    def latest_satellites(self, device_id: int) -> list[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT MAX(ts) AS ts FROM sat_samples WHERE device_id = ?", (device_id,)
        ).fetchone()
        if not row or row["ts"] is None:
            return []
        rows = self._conn.execute(
            "SELECT * FROM sat_samples WHERE device_id = ? AND ts = ? ORDER BY prn",
            (device_id, row["ts"]),
        ).fetchall()
        return [dict(r) for r in rows]

    def choose_span(self, seconds: int) -> str:
        """Pick the storage tier for a requested window.

        Raw up to a day, hourly to two months, daily beyond. The thresholds exist to
        bound the point count a chart has to draw, not to save storage.
        """
        if seconds <= RAW_MAX_SECONDS:
            return "raw"
        if seconds <= HOURLY_MAX_SECONDS:
            return "hour"
        return "day"

    def series(
        self,
        device_id: int,
        metric: str,
        seconds: int,
        now: int | None = None,
        expected_interval: float | None = None,
    ) -> Series:
        now = now or int(time.time())
        since = now - seconds
        span = self.choose_span(seconds)
        mid = self.metric_id(metric)

        if span == "raw":
            rows = self._conn.execute(
                "SELECT ts, value FROM samples "
                "WHERE device_id = ? AND metric_id = ? AND ts >= ? AND value IS NOT NULL "
                "ORDER BY ts",
                (device_id, mid, since),
            ).fetchall()
            ts = [int(r["ts"]) for r in rows]
            vals: list[float | None] = [r["value"] for r in rows]
            # The MEASURED cadence wins; the caller's expectation is only a fallback for
            # when there are too few points to estimate from. Backfilled history can
            # carry a different cadence from the live poller, and trusting the
            # configured value there turns every normal interval into a false gap.
            interval = _nominal_interval(ts) or (expected_interval or 0.0)
            return _with_gaps(Series(metric=metric, span="raw", ts=ts, value=vals), interval)

        rows = self._conn.execute(
            "SELECT bucket, min_value, avg_value, max_value FROM trends "
            "WHERE device_id = ? AND metric_id = ? AND span = ? AND bucket >= ? "
            "ORDER BY bucket",
            (device_id, mid, span, since),
        ).fetchall()
        series = Series(
            metric=metric,
            span=span,
            ts=[int(r["bucket"]) for r in rows],
            value=[r["avg_value"] for r in rows],
            min_value=[r["min_value"] for r in rows],
            max_value=[r["max_value"] for r in rows],
        )
        # Aggregate buckets have a known, fixed width, so no estimation is needed.
        return _with_gaps(series, 3600.0 if span == "hour" else 86400.0)

    def state_spans(
        self, device_id: int, metric: str, seconds: int, now: int | None = None
    ) -> list[dict[str, Any]]:
        """Contiguous runs of an enum metric, for shaded background bands (FR-16).

        Returned as runs rather than points because that is what a band renderer needs,
        and collapsing them here keeps the payload small over a long window.
        """
        now = now or int(time.time())
        since = now - seconds
        mid = self.metric_id(metric, kind="state")
        rows = self._conn.execute(
            "SELECT ts, text FROM samples "
            "WHERE device_id = ? AND metric_id = ? AND ts >= ? AND text IS NOT NULL "
            "ORDER BY ts",
            (device_id, mid, since),
        ).fetchall()
        spans: list[dict[str, Any]] = []
        for r in rows:
            if spans and spans[-1]["text"] == r["text"]:
                spans[-1]["end"] = int(r["ts"])
            else:
                spans.append({"start": int(r["ts"]), "end": int(r["ts"]), "text": r["text"]})
        return spans

    def satellite_history(
        self, device_id: int, seconds: int, now: int | None = None
    ) -> list[dict[str, Any]]:
        """Per-PRN positions over a window, for polar-chart trails (Q9)."""
        now = now or int(time.time())
        rows = self._conn.execute(
            "SELECT ts, prn, elevation, azimuth, signal, tracked FROM sat_samples "
            "WHERE device_id = ? AND ts >= ? AND elevation IS NOT NULL "
            "ORDER BY prn, ts",
            (device_id, now - seconds),
        ).fetchall()
        return [dict(r) for r in rows]


def _nominal_interval(ts: list[int]) -> float:
    """Estimate the sampling cadence from the data itself.

    Uses a high percentile rather than the median, because one window can contain more
    than one cadence: backfilled history recorded every 66 s sits alongside live data
    polled every 15 s. The median follows whichever is more numerous, so against a
    median of 15 s every single backfilled interval looks like an outage - which is
    exactly what happened, reporting 726 "gaps" over 24 hours, most of them 1.1 minutes
    long.

    The 90th percentile instead tolerates the slowest cadence actually present, while
    staying far below the minutes-to-hours scale of a real outage. It only misleads if
    genuine gaps make up more than a tenth of all intervals, at which point a higher
    threshold is arguably the right answer anyway.
    """
    if len(ts) < 3:
        return 0.0
    deltas = sorted(b - a for a, b in itertools.pairwise(ts))
    idx = min(int(len(deltas) * 0.9), len(deltas) - 1)
    return float(deltas[idx])


def _with_gaps(series: Series, interval: float) -> Series:
    """Record gaps and insert an explicit null so a chart breaks its line.

    Both halves are needed. The null is what makes the renderer stop drawing; the gap
    list is what lets it shade the missing region. Without the null, a chart joins the
    samples either side of an outage and presents it as measured data.
    """
    if interval <= 0 or len(series.ts) < 2:
        return series

    threshold = interval * GAP_FACTOR
    ts: list[int] = []
    value: list[float | None] = []
    mins: list[float | None] | None = [] if series.min_value is not None else None
    maxs: list[float | None] | None = [] if series.max_value is not None else None
    gaps: list[tuple[int, int]] = []

    for i, t in enumerate(series.ts):
        if i and (t - series.ts[i - 1]) > threshold:
            start, end = series.ts[i - 1], t
            gaps.append((start, end))
            # A null one interval after the last real sample breaks the line without
            # shifting the position of any measured point.
            ts.append(int(start + interval))
            value.append(None)
            if mins is not None:
                mins.append(None)
            if maxs is not None:
                maxs.append(None)
        ts.append(t)
        value.append(series.value[i])
        if mins is not None and series.min_value is not None:
            mins.append(series.min_value[i])
        if maxs is not None and series.max_value is not None:
            maxs.append(series.max_value[i])

    series.ts = ts
    series.value = value
    series.min_value = mins
    series.max_value = maxs
    series.gaps = gaps
    return series
