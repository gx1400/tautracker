"""Parser for the ``:SYST:STAT?`` screen (Q10).

The screen is fixed-width with **three** side-by-side panels, and its shape changes
with device state. Every one of the following cost a bug in the shell prototype:

* A whitespace split reads the *not-tracking* table's PRN as the *tracked* table's
  signal strength, silently producing plausible-but-wrong SS values.
* The tracked table and its ``SS`` header are **absent entirely** when tracking is 0.
* ``Suspended: <reason>`` prints on the line *below* ``MODE``, so a regex anchored to
  the MODE line never sees it.
* Position is labelled ``INIT`` (seed), ``AVG`` (surveying) or unlabelled (held).
* Vertical positions shift between states, so line numbers are not usable anchors.

Strategy therefore:

1. **Column boundaries are discovered, not hardcoded.** The right-hand panel begins
   where ``Time`` appears in the ``Satellite Status ... Time ...`` header; the tracked
   table ends two characters past ``SS`` in the satellite header.
2. **Scalars are anchored to their labels**, never to line numbers.
3. Unparseable fields return ``None`` and are counted, so a parse regression surfaces
   as a metric rather than as silently missing data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(slots=True)
class Satellite:
    prn: int
    elevation: int | None = None
    azimuth: int | None = None
    #: Arbitrary units, not dBm. Present only for tracked satellites.
    signal: int | None = None
    tracked: bool = False
    #: True when the row reads ``Acq .`` - the receiver is reading orbit data from it.
    acquiring: bool = False
    #: The ``*`` marker, meaning a channel is assigned and attempting to track.
    attempting: bool = False


@dataclass(slots=True)
class Health:
    self_test: bool | None = None
    int_pwr: bool | None = None
    oven_pwr: bool | None = None
    ocxo: bool | None = None
    efc: bool | None = None
    gps_rcv: bool | None = None
    overall: str | None = None


@dataclass(slots=True)
class Screen:
    """Everything extracted from one status screen."""

    sync_state: str | None = None
    acquisition_state: str | None = None
    mode: str | None = None
    tfom: int | None = None
    ffom: int | None = None
    #: 1PPS time interval vs GPS, nanoseconds. ``None`` while unlocked, where the
    #: screen prints ``--``. This is the primary accuracy metric.
    pps_ti_ns: float | None = None
    hold_threshold_us: float | None = None
    holdover_duration_s: int | None = None
    holdover_present_us: float | None = None
    holdover_predict_us_24h: float | None = None
    pps_clk_status: str | None = None
    tracking_count: int | None = None
    not_tracking_count: int | None = None
    satellites: list[Satellite] = field(default_factory=list)
    gps_time: str | None = None
    #: ``[?]`` present: the unit has not yet confirmed time from a satellite.
    time_unconfirmed: bool = False
    antenna_delay_ns: int | None = None
    elevation_mask_deg: int | None = None
    position_mode: str | None = None
    survey_percent: float | None = None
    survey_blocked: str | None = None
    #: Which position the screen showed: ``init``, ``avg``, ``hold`` or None.
    position_kind: str | None = None
    latitude: str | None = None
    longitude: str | None = None
    height_m: float | None = None
    health: Health = field(default_factory=Health)
    #: Field names that were expected but could not be parsed (FR-22 instrumentation).
    parse_failures: list[str] = field(default_factory=list)


_BRACKET = re.compile(r"\[\s*(.+?)\s*\]")
_SAT_HEADER = re.compile(r"PRN\s+El\s+Az")
_NUM = r"[-+]?\d+(?:\.\d+)?"


def _find_line(lines: list[str], needle: str) -> str | None:
    for line in lines:
        if needle in line:
            return line
    return None


def _right_panel_start(lines: list[str]) -> int:
    """Column where the right-hand panel begins.

    Anchored on the ``Satellite Status ... Time ...`` header, which is present in every
    captured screen regardless of state. Falling back to a fixed 46 keeps the parser
    working rather than failing outright if that header ever moves.
    """
    hdr = _find_line(lines, "Satellite Status")
    if hdr is not None:
        idx = hdr.find("Time")
        if idx > 0:
            return idx
    return 46


def parse_screen(text: str) -> Screen:  # noqa: C901 - a wide screen means many fields
    s = Screen()
    lines = text.splitlines()
    whole = "\n".join(lines)
    right = _right_panel_start(lines)

    def fail(name: str) -> None:
        s.parse_failures.append(name)

    # --- banner states, both inside [ ... ] ---
    for label, attr in (("SYNCHRONIZATION", "sync_state"), ("ACQUISITION", "acquisition_state")):
        line = _find_line(lines, label)
        if line and (m := _BRACKET.search(line)):
            setattr(s, attr, m.group(1))
        else:
            fail(attr)

    # --- SmartClock mode: the line marked '>>' ---
    for line in lines:
        if line.lstrip().startswith(">>"):
            body = line.lstrip()[2:]
            # The mode text is followed by 2+ spaces where the next panel begins.
            s.mode = re.split(r"\s{2,}", body.strip(), maxsplit=1)[0].strip() or None
            break
    if s.mode is None:
        fail("mode")

    # --- figures of merit ---
    if m := re.search(r"TFOM\s+(\d)", whole):
        s.tfom = int(m.group(1))
    else:
        fail("tfom")
    if m := re.search(r"FFOM\s+(\d)", whole):
        s.ffom = int(m.group(1))
    else:
        fail("ffom")

    # --- 1PPS TI: a signed value in ns, or '--' when unlocked ---
    if m := re.search(rf"1PPS TI\s+({_NUM})\s*ns", whole):
        s.pps_ti_ns = float(m.group(1))
    elif re.search(r"1PPS TI\s+--", whole):
        s.pps_ti_ns = None  # legitimately absent, not a parse failure
    else:
        fail("pps_ti_ns")

    if m := re.search(rf"HOLD THR\s+({_NUM})\s*us", whole):
        s.hold_threshold_us = float(m.group(1))
    if m := re.search(rf"Predict\s+({_NUM})\s*us", whole):
        s.holdover_predict_us_24h = float(m.group(1))
    if m := re.search(rf"Present\s+({_NUM})\s*us", whole):
        s.holdover_present_us = float(m.group(1))
    # 'Holdover Duration:  3m 16s' - also seen as '0m 06s'
    if m := re.search(r"Holdover Duration:\s*(?:(\d+)h\s*)?(?:(\d+)m\s*)?(\d+)s", whole):
        h, mi, sec = (int(g) if g else 0 for g in m.groups())
        s.holdover_duration_s = h * 3600 + mi * 60 + sec

    if m := re.search(r"1PPS CLK\s+(.+)", whole):
        s.pps_clk_status = m.group(1).strip()

    if m := re.search(r"Tracking:\s*(\d+)", whole):
        s.tracking_count = int(m.group(1))
    else:
        fail("tracking_count")
    if m := re.search(r"Not Tracking:\s*(\d+)", whole):
        s.not_tracking_count = int(m.group(1))

    # --- time: 'GPS      12:28:34     30 Mar 2007', optionally with [?] ---
    if m := re.search(r"(?:GPS|UTC|LOCAL)\s+(\d\d:\d\d:\d\d)\s*(\[\?\])?\s*(\d{1,2} \w{3} \d{4})", whole):
        s.gps_time = f"{m.group(1)} {m.group(3)}"
        s.time_unconfirmed = m.group(2) is not None
    else:
        fail("gps_time")

    if m := re.search(r"ANT DLY\s+(\d+)\s*ns", whole):
        s.antenna_delay_ns = int(m.group(1))
    if m := re.search(r"ELEV MASK\s+(\d+)\s*deg", whole):
        s.elevation_mask_deg = int(m.group(1))

    # --- position mode, and the suspend reason that lives on the NEXT line ---
    if m := re.search(r"MODE\s+(.+)", whole):
        s.position_mode = re.sub(r"\s+", " ", m.group(1)).strip()
        if pm := re.search(r"Survey:\s*<?([\d.]+)\s*%", s.position_mode):
            s.survey_percent = float(pm.group(1))
        elif "Hold" in s.position_mode:
            s.survey_percent = None
    if m := re.search(r"Suspended:\s*(.+)", whole):
        s.survey_blocked = m.group(1).strip()

    # --- position, which carries a different label per survey state ---
    for kind, prefix in (("init", "INIT "), ("avg", "AVG "), ("hold", "")):
        lat = re.search(rf"{prefix}LAT\s+([NS])\s+(\d+):(\d+):([\d.]+)", whole)
        if lat:
            s.position_kind = kind
            s.latitude = f"{lat.group(1)} {lat.group(2)}:{lat.group(3)}:{lat.group(4)}"
            if lon := re.search(rf"{prefix}LON\s+([EW])\s+(\d+):(\d+):([\d.]+)", whole):
                s.longitude = f"{lon.group(1)} {lon.group(2)}:{lon.group(3)}:{lon.group(4)}"
            if hgt := re.search(rf"{prefix}HGT\s+({_NUM})\s*m", whole):
                s.height_m = float(hgt.group(1))
            break

    # --- health ---
    if line := _find_line(lines, "HEALTH MONITOR"):
        if m := _BRACKET.search(line):
            s.health.overall = m.group(1)
    for label, attr in (
        ("Self Test", "self_test"),
        ("Int Pwr", "int_pwr"),
        ("Oven Pwr", "oven_pwr"),
        ("OCXO", "ocxo"),
        ("EFC", "efc"),
        ("GPS Rcv", "gps_rcv"),
    ):
        if m := re.search(rf"{label}:\s*(\w+)", whole):
            setattr(s.health, attr, m.group(1).upper() == "OK")

    s.satellites = _parse_satellites(lines, right)
    return s


def _parse_satellites(lines: list[str], right: int) -> list[Satellite]:
    """Extract both satellite tables using discovered column boundaries.

    The tracked table occupies the leftmost columns and ends two characters past the
    ``SS`` header. Everything between there and the right-hand panel is the
    not-tracking table, which itself may hold two sub-columns. Slicing by column is
    what prevents the not-tracking PRN from being misread as a signal strength.
    """
    hdr_idx = next((i for i, ln in enumerate(lines) if _SAT_HEADER.search(ln)), None)
    if hdr_idx is None:
        return []
    header = lines[hdr_idx]

    ss_pos = header.find("SS")
    # +2 covers the two header characters; a three-digit SS is right-aligned to end at
    # that same column, so the slice still contains it.
    tracked_end = ss_pos + 2 if ss_pos != -1 else 0

    sats: list[Satellite] = []
    for raw in lines[hdr_idx + 1 :]:
        if not raw.strip():
            continue
        # The satellite block ends at the ELEV MASK / attempting-to-track footer line.
        if "ELEV MASK" in raw or "attempting to track" in raw:
            left_part = raw[:tracked_end] if tracked_end else ""
            if left_part.strip() and "ELEV MASK" not in left_part:
                sats.extend(_tracked_rows(left_part))
            break
        if "HEALTH MONITOR" in raw:
            break

        if tracked_end:
            sats.extend(_tracked_rows(raw[:tracked_end]))
        middle = raw[tracked_end:right]
        sats.extend(_untracked_rows(middle))

    # Deduplicate, preferring the tracked record if a PRN somehow appears twice.
    best: dict[int, Satellite] = {}
    for sat in sats:
        prev = best.get(sat.prn)
        if prev is None or (sat.tracked and not prev.tracked):
            best[sat.prn] = sat
    return sorted(best.values(), key=lambda x: x.prn)


_TRACKED_ROW = re.compile(r"^\s{0,2}(\d{1,2})\s+(\d{1,3})\s+(\d{1,3})\s+(\d{1,3})\s*$")


def _tracked_rows(segment: str) -> list[Satellite]:
    m = _TRACKED_ROW.match(segment)
    if not m:
        return []
    prn, el, az, ss = (int(g) for g in m.groups())
    return [Satellite(prn=prn, elevation=el, azimuth=az, signal=ss, tracked=True)]


#: A not-tracking entry: optional '*', PRN, then either El/Az, 'Acq .', or '-- ---'.
_UNTRACKED = re.compile(
    r"(\*)?\s*(\d{1,2})\s+(?:(Acq\s*\.+)|(--)\s+(---)|(\d{1,3})\s+(\d{1,3}))"
)


def _untracked_rows(segment: str) -> list[Satellite]:
    out: list[Satellite] = []
    for m in _UNTRACKED.finditer(segment):
        star, prn, acq, _dash_el, _dash_az, el, az = m.groups()
        out.append(
            Satellite(
                prn=int(prn),
                elevation=int(el) if el else None,
                azimuth=int(az) if az else None,
                signal=None,
                tracked=False,
                acquiring=acq is not None,
                attempting=star is not None,
            )
        )
    return out
