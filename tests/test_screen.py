"""Parser tests against real captured screens.

Every fixture is a verbatim ``:SYST:STAT?`` response from an HP Z3801A
(S/N 3542A05653, firmware 3543-A), chosen to cover the state variants that each broke
the shell prototype at least once. See docs/open-questions.md Q10.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tautrack.device.screen import parse_screen

FIXTURES = Path(__file__).parent / "fixtures" / "screens"
ALL_SCREENS = sorted(FIXTURES.glob("*.txt"))


def load(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", ALL_SCREENS, ids=[p.stem for p in ALL_SCREENS])
def test_no_parse_failures(path: Path) -> None:
    """No expected field should fail to parse on any captured screen.

    This is the regression guard. A parser change that silently stops extracting a
    field fails here rather than quietly writing nulls to the database.
    """
    screen = parse_screen(path.read_text(encoding="utf-8"))
    assert screen.parse_failures == []


def test_locked_hold_tracked() -> None:
    """Survey complete, in Hold, fully settled - the target state."""
    s = parse_screen(load("locked_hold_tracked"))
    assert s.sync_state == "Outputs Valid"
    assert s.mode == "Locked to GPS"
    assert s.tfom == 3
    assert s.ffom == 0
    assert s.pps_ti_ns == pytest.approx(-104.5)
    assert s.holdover_predict_us_24h == pytest.approx(176.9)
    assert s.hold_threshold_us == pytest.approx(1.0)
    assert s.position_mode == "Hold"
    assert s.survey_percent is None
    assert s.position_kind == "hold"
    assert s.latitude == "N 38:59:56.449"
    assert s.longitude == "W 94:40:55.754"
    assert s.height_m == pytest.approx(325.90)
    assert s.time_unconfirmed is False
    assert s.health.overall == "OK"
    assert all(
        getattr(s.health, f) is True
        for f in ("self_test", "int_pwr", "oven_pwr", "ocxo", "efc", "gps_rcv")
    )


def test_tracked_satellites_have_signal_not_a_neighbouring_prn() -> None:
    """The bug this parser exists to prevent.

    A whitespace split on these lines reads the not-tracking table's PRN as the
    tracked table's signal strength. Signal strengths are large (tens to ~170) while
    PRNs are 1-32, so a misparse shows up as implausibly small SS values.
    """
    s = parse_screen(load("locked_hold_tracked"))
    tracked = [x for x in s.satellites if x.tracked]
    assert tracked, "expected at least one tracked satellite"
    for sat in tracked:
        assert sat.signal is not None
        assert sat.elevation is not None and 0 <= sat.elevation <= 90
        assert sat.azimuth is not None and 0 <= sat.azimuth <= 360
        # A PRN misread as SS would land in 1..32; real SS here is well above that.
        assert sat.signal > 32, f"PRN {sat.prn} signal {sat.signal} looks like a PRN"


def test_zero_tracked_has_no_ss_column() -> None:
    """With nothing tracked the SS header and the whole left table are absent."""
    s = parse_screen(load("holdover_zero_tracked"))
    assert s.tracking_count == 0
    assert not [x for x in s.satellites if x.tracked]
    # The not-tracking table must still yield satellites.
    assert s.satellites, "expected not-tracking satellites to be parsed"
    assert all(x.signal is None for x in s.satellites)


def test_survey_in_progress_uses_avg_position() -> None:
    s = parse_screen(load("survey_avg_tracked"))
    assert s.position_kind == "avg"
    assert s.survey_percent is not None
    assert "Survey" in (s.position_mode or "")


def test_powerup_init_position_and_unresolved_elevations() -> None:
    """Straight after a power cycle: INIT position, and El/Az shown as ``-- ---``."""
    s = parse_screen(load("powerup_init_nofix"))
    assert s.position_kind == "init"
    assert s.time_unconfirmed is True, "the [?] flag should be detected"
    # Satellites with '-- ---' are listed but have no elevation yet.
    assert any(x.elevation is None for x in s.satellites)


def test_suspend_reason_is_on_the_line_below_mode() -> None:
    """``Suspended:`` is a separate line; a MODE-anchored regex misses it."""
    s = parse_screen(load("survey_init_tracked"))
    assert s.survey_blocked is not None
    assert "sats" in s.survey_blocked or "geometry" in s.survey_blocked


def test_pps_ti_absent_while_unlocked_is_not_a_failure() -> None:
    """The screen prints ``1PPS TI  --`` when unlocked; that is data, not an error."""
    s = parse_screen(load("holdover_zero_tracked"))
    assert s.pps_ti_ns is None
    assert "pps_ti_ns" not in s.parse_failures


def test_pps_clk_status_is_not_the_acquisition_banner() -> None:
    """``1PPS CLK`` appears twice on the screen.

    Once inside the ACQUISITION banner's brackets (``[ GPS 1PPS CLK Valid ]``) and once
    as a right-panel field. A whole-screen search finds the banner first and returns
    the useless fragment ``Valid ]``.
    """
    s = parse_screen(load("locked_hold_tracked"))
    assert s.pps_clk_status is not None
    assert not s.pps_clk_status.endswith("]"), f"matched the banner: {s.pps_clk_status!r}"
    assert "Synchronized" in s.pps_clk_status or "Invalid" in s.pps_clk_status


def test_pps_ti_in_picoseconds_is_scaled_to_nanoseconds() -> None:
    """The device switches to picoseconds once the offset drops below 1 ns.

    This matters more than it looks: the picosecond readings happen precisely when the
    oscillator is performing best, so treating ``+300 ps`` as 300 would paint 1000x
    spikes onto the accuracy chart exactly where accuracy is highest. Across one
    42-hour capture the split was 1380 ns, 29 ps, 565 unlocked.
    """
    s = parse_screen(load("locked_ti_picoseconds"))
    assert s.pps_ti_ns == pytest.approx(0.3)
    assert s.parse_failures == []
