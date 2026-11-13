"""ser2net config compatibility.

The emulator's value is that a real ser2net config runs unchanged, so the parsing is
what is worth testing - particularly the serial parameter notation, where a silent
fallback to 8-N-1 would return garbage that looks like a dead instrument.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "ser2net_emu", Path(__file__).resolve().parents[1] / "tools" / "ser2net_emu.py"
)
assert _spec and _spec.loader
ser2net_emu = importlib.util.module_from_spec(_spec)
sys.modules["ser2net_emu"] = ser2net_emu
_spec.loader.exec_module(ser2net_emu)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("19200o71", {"baudrate": 19200, "parity": "O", "bytesize": 7, "stopbits": 1}),
        ("9600n81", {"baudrate": 9600, "parity": "N", "bytesize": 8, "stopbits": 1}),
        ("115200e72", {"baudrate": 115200, "parity": "E", "bytesize": 7, "stopbits": 2}),
    ],
)
def test_serial_notation_matches_ser2net(spec: str, expected: dict) -> None:
    assert ser2net_emu.parse_serial_params(spec) == expected


@pytest.mark.parametrize("bad", ["19200", "19200x81", "", "19200o91", "o71"])
def test_malformed_serial_spec_is_rejected_loudly(bad: str) -> None:
    """Falling back to a default here would look like a dead instrument, not a typo."""
    with pytest.raises(ValueError, match="serial parameters"):
        ser2net_emu.parse_serial_params(bad)


def test_parses_a_real_ser2net_connection() -> None:
    conn = ser2net_emu.parse_connection(
        "gpsdo",
        {
            "accepter": "tcp,4196",
            "connector": "serialdev,COM7,19200o71,local",
            "options": {"max-connections": 1},
        },
    )
    assert (conn.listen_port, conn.device) == (4196, "COM7")
    assert (conn.baudrate, conn.parity, conn.bytesize, conn.stopbits) == (19200, "O", 7, 1)
    assert conn.max_connections == 1
    # Round-trips back to ser2net's own notation.
    assert conn.serial_spec == "19200o71"


def test_accepter_with_explicit_host() -> None:
    conn = ser2net_emu.parse_connection(
        "x", {"accepter": "tcp,127.0.0.1,5000", "connector": "serialdev,/dev/ttyUSB0,9600n81"}
    )
    assert (conn.listen_host, conn.listen_port) == ("127.0.0.1", 5000)


def test_telnet_accepter_is_flagged_rather_than_silently_raw() -> None:
    """RFC 2217 is not emulated; the caller must be told, not quietly downgraded."""
    conn = ser2net_emu.parse_connection(
        "t", {"accepter": "telnet,tcp,4196", "connector": "serialdev,COM7,19200o71"}
    )
    assert conn.telnet_requested is True
    assert conn.listen_port == 4196


def test_max_connections_defaults_to_one() -> None:
    """ser2net's own default is higher, but SCPI tolerates exactly one reader (C-2)."""
    conn = ser2net_emu.parse_connection(
        "x", {"accepter": "tcp,4196", "connector": "serialdev,COM7,19200o71"}
    )
    assert conn.max_connections == 1


def test_unsupported_connector_is_rejected() -> None:
    with pytest.raises(ValueError, match="serialdev"):
        ser2net_emu.parse_connection(
            "x", {"accepter": "tcp,4196", "connector": "tcp,somewhere,1234"}
        )


def test_repo_config_loads(tmp_path) -> None:
    """The checked-in ser2net.yaml must be valid for both the emulator and ser2net."""
    cfg = Path(__file__).resolve().parents[1] / "ser2net.yaml"
    if not cfg.exists():
        pytest.skip("ser2net.yaml not present")
    conns = ser2net_emu.load_config(cfg)
    assert len(conns) == 1
    assert conns[0].serial_spec == "19200o71"
    assert conns[0].banner is None, "a banner would corrupt the first SCPI reply"
