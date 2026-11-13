"""Discrete query parsing.

The unit conversion here is not cosmetic: the same metric is fed by two sources with
different units, and getting it wrong produces plausible-looking but wrong values
rather than an obvious failure.
"""

from __future__ import annotations

import pytest

from tautrack.device.queries import FAST_QUERIES, _count_list, _first_number_of_pair, _number


def q(command: str):
    return next(x for x in FAST_QUERIES if x.command == command)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("+4.15E-008", 41.5),  # locked, measured against '+41.5 ns' on the screen
        ("-3.6E-009", -3.6),
        ("+2.18E-008", 21.8),
    ],
)
def test_tint_seconds_are_converted_to_nanoseconds(body: str, expected: float) -> None:
    """``:SYNC:TINT?`` answers in seconds; the screen prints nanoseconds.

    Both write ``pps_ti_ns``. Without conversion the series silently mixes values a
    billion times apart.
    """
    assert q(":SYNC:TINT?").parse(body) == pytest.approx(expected)


def test_tint_stale_is_expected_not_a_fault() -> None:
    """E-230 during holdover is a legitimate device state, not an error to alarm on."""
    assert q(":SYNC:TINT?").stale_is_normal is True


def test_holdover_duration_takes_the_first_field() -> None:
    """``:SYNC:HOLD:DUR?`` answers ``+1.52000E+002,1`` - seconds, then a flag."""
    assert _first_number_of_pair("+1.52000E+002,1") == pytest.approx(152.0)


def test_tracked_list_counts_entries_not_the_zero_sentinel() -> None:
    """``:GPS:SAT:TRAC?`` answers ``+0`` when empty, but ``+1,+2`` when tracking two."""
    assert _count_list("+0") == 0
    assert _count_list("+1,+2") == 2
    assert _count_list("+11,+22,+31") == 3


def test_number_handles_scpi_exponent_format() -> None:
    assert _number("+5.43933E+001") == pytest.approx(54.3933)
    assert _number("+809465") == 809465
    assert _number("") is None


def test_every_fast_query_has_exactly_one_parser() -> None:
    """A query with neither parser silently discards its reply."""
    for item in FAST_QUERIES:
        assert (item.parse is None) != (item.text_parse is None), item.command
