#!/usr/bin/env python3
"""Import a captured shell-prototype session into tautrack's database.

Replay writes samples at *current* timestamps, which is right for development but
leaves the long-span charts empty. This imports the original capture at its **real**
timestamps, so a year view has something in it from day one.

Two sources, because they carry different things:

* ``status_history.txt`` - verbatim ``:SYST:STAT?`` screens with timestamps. Re-parsed
  through the production parser, so this also exercises it over every captured state.
* ``gpsdo_fast.csv`` - the discrete-query results, which include 1PPS TI and the raw
  EFC DAC count that the screen does not carry.

    python tools/backfill.py --history ../gpsdo/status_history.txt \
        --fast ../gpsdo/gpsdo_fast.csv --db data/tautrack.db --tz America/Chicago
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tautrack.device.queries import FAST_QUERIES
from tautrack.device.screen import parse_screen
from tautrack.poller import _screen_texts, _screen_values, _signature
from tautrack.store.db import Store
from tautrack.store.rollup import housekeep

SCREEN_SPLIT = re.compile(r"^===== (\S+) =====$", re.M)

#: CSV column -> the query whose reply it holds, so the same parsers run on both paths.
FAST_COLUMNS = {
    "sync_stat": ":SYNC:STAT?",
    "tfom": ":SYNC:TFOM?",
    "ffom": ":SYNC:FFOM?",
    "hold_dur": ":SYNC:HOLD:DUR?",
    "trac_count": ":GPS:SAT:TRAC:COUN?",
    "efc_pct": ":DIAG:ROSC:EFC?",
    "efc_abs": ":DIAG:ROSC:EFC:ABS?",
    "outp_stat": ":OUTP:STAT?",
    "log_count": ":DIAG:LOG:COUN?",
    "tint": ":SYNC:TINT?",
}
QUERY_BY_COMMAND = {q.command: q for q in FAST_QUERIES}


def to_epoch(stamp: str, tz: ZoneInfo) -> int | None:
    """The prototype wrote naive local-time ISO stamps, so attach the zone."""
    try:
        return int(datetime.fromisoformat(stamp).replace(tzinfo=tz).timestamp())
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history", type=Path)
    ap.add_argument("--fast", type=Path)
    ap.add_argument("--db", type=Path, default=Path("data/tautrack.db"))
    ap.add_argument("--tz", default="UTC", help="zone the captured timestamps were written in")
    ap.add_argument("--serial", default="3542A05653")
    ap.add_argument("--model", default="Z3801A")
    ap.add_argument("--no-rollup", action="store_true")
    args = ap.parse_args()

    tz = ZoneInfo(args.tz)
    store = Store(args.db)
    device = store.upsert_device(
        {
            "manufacturer": "HEWLETT-PACKARD",
            "model": args.model,
            "serial": args.serial,
            "firmware": "3543-A",
        }
    )

    screens = sats = failures = 0
    if args.history and args.history.exists():
        text = args.history.read_text(encoding="utf-8", errors="replace")
        chunks = SCREEN_SPLIT.split(text)
        for i in range(1, len(chunks) - 1, 2):
            ts = to_epoch(chunks[i], tz)
            if ts is None:
                continue
            body = chunks[i + 1]
            screen = parse_screen(body)
            if screen.parse_failures:
                failures += 1
            store.write_samples(device, ts, _screen_values(screen), _screen_texts(screen))
            if screen.satellites:
                store.write_satellites(device, ts, screen.satellites)
                sats += len(screen.satellites)
            store.write_screen(device, ts, _signature(screen), body.strip("\n"))
            screens += 1
        print(f"screens imported : {screens}  (satellite rows {sats}, parse failures {failures})")

    fast_rows = 0
    if args.fast and args.fast.exists():
        with args.fast.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                ts = to_epoch((row.get("pc_time") or "").strip(), tz)
                if ts is None:
                    continue
                values: dict[str, float | None] = {}
                texts: dict[str, str] = {}
                for column, command in FAST_COLUMNS.items():
                    raw = (row.get(column) or "").strip().strip('"')
                    if not raw:
                        continue
                    q = QUERY_BY_COMMAND.get(command)
                    if q is None:
                        continue
                    # Reuse the production parsers so the backfill cannot drift from
                    # live polling - notably the seconds-to-nanoseconds TI conversion.
                    if q.text_parse is not None:
                        if (t := q.text_parse(raw)) is not None:
                            texts[q.metric] = t
                    elif q.parse is not None:
                        values[q.metric] = q.parse(raw)
                store.write_samples(device, ts, values, texts)
                fast_rows += 1
        print(f"fast rows imported: {fast_rows}")

    if not args.no_rollup:
        stats = housekeep(store.connection, raw_days=14, hourly_days=400, daily_days=3650)
        print(f"rollup: {stats}")

    row = store.connection.execute(
        "SELECT MIN(ts) AS a, MAX(ts) AS b, COUNT(*) AS n FROM samples"
    ).fetchone()
    if row and row["n"]:
        span_h = (row["b"] - row["a"]) / 3600
        print(
            f"database now holds {row['n']} samples spanning {span_h:.1f} h "
            f"({datetime.fromtimestamp(row['a'], tz)} -> {datetime.fromtimestamp(row['b'], tz)})"
        )
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
