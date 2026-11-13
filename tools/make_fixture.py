#!/usr/bin/env python3
"""Build a replay fixture (Q11) from a captured monitor session.

Reads the shell prototype's ``status_history.txt`` (verbatim ``:SYST:STAT?`` screens)
and ``gpsdo_fast.csv`` (discrete query results) and emits JSONL that the replay
transport serves back. The result is real device behaviour, so development and CI
exercise the genuine screen variants rather than invented ones.

    python tools/make_fixture.py \
        --history ../gpsdo/status_history.txt \
        --fast ../gpsdo/gpsdo_fast.csv \
        --out tests/fixtures/session.jsonl --limit 400
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

SCREEN_SPLIT = re.compile(r"^===== (\S+) =====$", re.M)

#: gpsdo_fast.csv column -> the SCPI query whose reply it holds.
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history", type=Path, required=True)
    ap.add_argument("--fast", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--limit", type=int, default=400, help="max screens to include")
    ap.add_argument("--idn", default="HEWLETT-PACKARD,Z3801A,3542A05653,3543-A")
    args = ap.parse_args()

    records: list[dict[str, str]] = [{"command": "*IDN?", "body": args.idn, "prompt": "scpi >"}]

    text = args.history.read_text(encoding="utf-8", errors="replace")
    chunks = SCREEN_SPLIT.split(text)
    screens = [chunks[i + 1].strip("\n") for i in range(1, len(chunks) - 1, 2)]
    # Take the most recent, which reflect the current configuration.
    for body in screens[-args.limit :]:
        records.append({"command": ":SYST:STAT?", "body": body, "prompt": "scpi >"})

    if args.fast and args.fast.exists():
        with args.fast.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
        for row in rows[-args.limit :]:
            for column, command in FAST_COLUMNS.items():
                raw = (row.get(column) or "").strip().strip('"')
                if raw == "":
                    # An empty TI column means the device answered E-230: recognised
                    # command, no valid data. Preserving that keeps the stale-data
                    # path on the tested route.
                    if command in (":SYNC:TINT?", ":PTIM:TINT?"):
                        records.append({"command": command, "body": "", "prompt": "E-230>"})
                    continue
                records.append({"command": command, "body": raw, "prompt": "scpi >"})

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    by_cmd: dict[str, int] = {}
    for r in records:
        by_cmd[r["command"]] = by_cmd.get(r["command"], 0) + 1
    print(f"wrote {len(records)} records to {args.out}")
    for cmd, n in sorted(by_cmd.items(), key=lambda kv: -kv[1]):
        print(f"  {cmd:<24} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
