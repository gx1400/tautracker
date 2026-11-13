"""Housekeeping: roll raw samples into trends, then prune (Q3).

The Zabbix history/trends model. Two rules shape the implementation:

* **Aggregates keep min and max** (FR-10). Averaging alone would erase a ten-minute
  holdover from a one-year view - the single event an operator most needs to find.
* **Roll up before pruning, and only prune what has been rolled up.** Deleting first,
  or pruning a bucket that was never aggregated, loses data irrecoverably.

Per-satellite aggregates additionally keep the **appearance count**, because how often
a PRN was visible is as diagnostic as how strong it was (Q9).
"""

from __future__ import annotations

import sqlite3
import time

HOUR = 3600
DAY = 86400

SPAN_SECONDS = {"hour": HOUR, "day": DAY}


def _bucket_expr(span: str) -> int:
    try:
        return SPAN_SECONDS[span]
    except KeyError:
        raise ValueError(f"unknown span: {span}") from None


def rollup_scalars(conn: sqlite3.Connection, span: str, now: int | None = None) -> int:
    """Aggregate raw samples into ``trends`` for one span.

    Only *closed* buckets are aggregated: the current partial bucket is skipped so it
    is never written with an incomplete count and then left stale.
    """
    now = now or int(time.time())
    width = _bucket_expr(span)
    current_bucket = (now // width) * width

    cur = conn.execute(
        """
        INSERT INTO trends (device_id, metric_id, span, bucket, count, min_value, avg_value, max_value)
        SELECT device_id,
               metric_id,
               ?,
               (ts / ?) * ?            AS bucket,
               COUNT(value),
               MIN(value),
               AVG(value),
               MAX(value)
        FROM samples
        WHERE value IS NOT NULL
          AND ts < ?
        GROUP BY device_id, metric_id, bucket
        ON CONFLICT(device_id, metric_id, span, bucket) DO UPDATE SET
            count     = excluded.count,
            min_value = excluded.min_value,
            avg_value = excluded.avg_value,
            max_value = excluded.max_value
        """,
        (span, width, width, current_bucket),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def rollup_satellites(conn: sqlite3.Connection, span: str, now: int | None = None) -> int:
    now = now or int(time.time())
    width = _bucket_expr(span)
    current_bucket = (now // width) * width

    cur = conn.execute(
        """
        INSERT INTO sat_trends (device_id, span, bucket, prn, count, tracked_count,
                                min_signal, avg_signal, max_signal,
                                min_elevation, max_elevation)
        SELECT device_id,
               ?,
               (ts / ?) * ?  AS bucket,
               prn,
               COUNT(*),
               SUM(tracked),
               MIN(signal),
               AVG(signal),
               MAX(signal),
               MIN(elevation),
               MAX(elevation)
        FROM sat_samples
        WHERE ts < ?
        GROUP BY device_id, bucket, prn
        ON CONFLICT(device_id, span, bucket, prn) DO UPDATE SET
            count         = excluded.count,
            tracked_count = excluded.tracked_count,
            min_signal    = excluded.min_signal,
            avg_signal    = excluded.avg_signal,
            max_signal    = excluded.max_signal,
            min_elevation = excluded.min_elevation,
            max_elevation = excluded.max_elevation
        """,
        (span, width, width, current_bucket),
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def prune(
    conn: sqlite3.Connection,
    raw_days: int,
    hourly_days: int,
    daily_days: int,
    now: int | None = None,
) -> dict[str, int]:
    """Delete data past its retention, newest tier first.

    Raw is pruned only after the rollup has run, so nothing is discarded before it has
    been aggregated.
    """
    now = now or int(time.time())
    deleted: dict[str, int] = {}

    raw_cutoff = now - raw_days * DAY
    deleted["samples"] = conn.execute("DELETE FROM samples WHERE ts < ?", (raw_cutoff,)).rowcount
    deleted["sat_samples"] = conn.execute(
        "DELETE FROM sat_samples WHERE ts < ?", (raw_cutoff,)
    ).rowcount

    hourly_cutoff = now - hourly_days * DAY
    deleted["trends_hour"] = conn.execute(
        "DELETE FROM trends WHERE span = 'hour' AND bucket < ?", (hourly_cutoff,)
    ).rowcount
    deleted["sat_trends_hour"] = conn.execute(
        "DELETE FROM sat_trends WHERE span = 'hour' AND bucket < ?", (hourly_cutoff,)
    ).rowcount

    daily_cutoff = now - daily_days * DAY
    deleted["trends_day"] = conn.execute(
        "DELETE FROM trends WHERE span = 'day' AND bucket < ?", (daily_cutoff,)
    ).rowcount
    deleted["sat_trends_day"] = conn.execute(
        "DELETE FROM sat_trends WHERE span = 'day' AND bucket < ?", (daily_cutoff,)
    ).rowcount

    # Events and screens follow the raw retention; they are small but unbounded.
    deleted["events"] = conn.execute("DELETE FROM events WHERE ts < ?", (raw_cutoff,)).rowcount
    return {k: max(v, 0) for k, v in deleted.items()}


def housekeep(
    conn: sqlite3.Connection,
    raw_days: int = 14,
    hourly_days: int = 400,
    daily_days: int = 3650,
    now: int | None = None,
) -> dict[str, int]:
    """One full cycle: aggregate every span, then prune.

    Ordering is deliberate - rolling up before pruning is what makes the prune safe.
    """
    now = now or int(time.time())
    stats: dict[str, int] = {}
    with conn:
        for span in ("hour", "day"):
            stats[f"rollup_{span}"] = rollup_scalars(conn, span, now)
            stats[f"rollup_sat_{span}"] = rollup_satellites(conn, span, now)
        stats.update(prune(conn, raw_days, hourly_days, daily_days, now))
    return stats
