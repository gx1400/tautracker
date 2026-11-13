"""Discrete query definitions and their parsers (Q10).

Probing firmware 3543-A showed 21 queries return structured values in ~0.20 s each,
against ~2.84 s for the whole status screen. So the fast poll reads these and does no
column parsing at all; the screen is reserved for what has no discrete form.

See docs/command-map.md for the full probe results.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _number(body: str) -> float | None:
    """First numeric token. Replies arrive as ``+3`` or ``+5.43933E+001``."""
    m = _NUM.search(body)
    return float(m.group()) if m else None


def _first_number_of_pair(body: str) -> float | None:
    """``:SYNC:HOLD:DUR?`` answers ``+1.52000E+002,1`` - seconds, then a flag."""
    return _number(body.split(",")[0]) if body else None


def _count_list(body: str) -> float | None:
    """``:GPS:SAT:TRAC?`` answers ``+0`` when empty, or ``+1,+2`` when tracking."""
    if not body:
        return None
    items = [p for p in body.split(",") if p.strip()]
    if len(items) == 1 and _number(items[0]) == 0:
        return 0.0
    return float(len(items))


def _seconds_to_ns(body: str) -> float | None:
    """``:SYNC:TINT?`` answers in SECONDS; the status screen prints NANOSECONDS.

    Measured on firmware 3543-A while locked: ``+4.15E-008`` from the query against
    ``+41.5 ns`` on the screen. Both feed the same metric, so without this conversion
    the series would mix values differing by a factor of 1e9 - which looks like a
    sub-nanosecond reading rather than an obvious error.
    """
    value = _number(body)
    return None if value is None else value * 1e9


def _text(body: str) -> str | None:
    return body.strip() or None


@dataclass(frozen=True, slots=True)
class Query:
    command: str
    metric: str
    parse: Callable[[str], float | None] | None = None
    #: Enum-valued metrics are stored as text, not coerced to a number.
    text_parse: Callable[[str], str | None] | None = None
    unit: str | None = None
    #: True when an E-230 (data stale) reply is an expected state rather than a fault.
    stale_is_normal: bool = False


#: Fast poll. Nine queries at ~0.2 s each fits comfortably inside a 15 s cadence.
FAST_QUERIES: tuple[Query, ...] = (
    Query(":SYNC:STAT?", "sync_state", text_parse=_text),
    Query(":SYNC:TFOM?", "tfom", parse=_number),
    Query(":SYNC:FFOM?", "ffom", parse=_number),
    Query(":SYNC:HOLD:DUR?", "holdover_duration_s", parse=_first_number_of_pair, unit="s"),
    Query(":GPS:SAT:TRAC:COUN?", "satellites_tracked", parse=_number),
    Query(":DIAG:ROSC:EFC?", "efc_percent", parse=_number, unit="%"),
    # Absolute DAC count: finer resolution than the percent form and an integer, which
    # makes it the better series for multi-year oscillator aging trends.
    Query(":DIAG:ROSC:EFC:ABS?", "efc_dac", parse=_number),
    # Recorded as output_state rather than "outputs_valid": the device answered 0
    # while the status screen simultaneously read "Outputs Valid", so this field's
    # semantics are not established. Naming it neutrally avoids asserting a meaning
    # the probe did not confirm.
    Query(":OUTP:STAT?", "output_state", parse=_number),
    Query(":DIAG:LOG:COUN?", "diag_log_count", parse=_number),
    # 1PPS time interval - the primary accuracy metric, available on the fast poll at
    # ~0.2 s rather than waiting ~2.8 s for the status screen.
    #
    # Returns E-230 (data stale) while unlocked, which is a legitimate state and not a
    # fault; confirmed to return a value once locked. Reply is in SECONDS, so it is
    # scaled to nanoseconds to match the screen's units.
    Query(":SYNC:TINT?", "pps_ti_ns", parse=_seconds_to_ns, unit="ns", stale_is_normal=True),
)

#: Read once per connection (FR-5).
IDENTITY_QUERY = "*IDN?"

#: Slow poll: the only command that yields per-satellite tables and the rest.
STATUS_QUERY = ":SYST:STAT?"
