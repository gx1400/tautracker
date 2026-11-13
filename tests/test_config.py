"""Config: TOML file, environment overrides, and their precedence (Q8)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tautrack.config import Settings, load_settings


def write(tmp_path, body: str):
    p = tmp_path / "config.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_defaults_target_the_z3801a_line_settings() -> None:
    """7-O-1 is unusual - most equipment is 8-N-1 - so the default must be right."""
    s = Settings()
    assert (s.transport.serial.bytesize, s.transport.serial.parity) == (7, "odd")
    assert s.transport.serial.baudrate == 19200


def test_web_binds_loopback_by_default() -> None:
    """There is no authentication (NFR-6), so exposure must be deliberate."""
    assert Settings().web.host == "127.0.0.1"


def test_toml_values_are_loaded(tmp_path) -> None:
    cfg = write(tmp_path, '[transport]\nkind = "tcp"\n\n[transport.tcp]\nport = 5000\n')
    s = load_settings(cfg)
    assert s.transport.kind == "tcp"
    assert s.transport.tcp.port == 5000


def test_environment_overrides_the_file(tmp_path, monkeypatch) -> None:
    cfg = write(tmp_path, "[web]\nport = 8080\n")
    monkeypatch.setenv("TAUTRACK_WEB__PORT", "9999")
    assert load_settings(cfg).web.port == 9999


def test_environment_overrides_defaults_without_a_file(monkeypatch) -> None:
    monkeypatch.setenv("TAUTRACK_WEB__HOST", "0.0.0.0")
    assert load_settings(None).web.host == "0.0.0.0"


def test_missing_file_falls_back_to_defaults(tmp_path) -> None:
    """A container with no mounted config must still start."""
    assert load_settings(tmp_path / "absent.toml").web.port == 8080


def test_unknown_keys_are_rejected(tmp_path) -> None:
    """A typo in a config key should fail loudly, not be silently ignored."""
    cfg = write(tmp_path, "typpo = 1\n")
    with pytest.raises(ValidationError):
        load_settings(cfg)


@pytest.mark.parametrize(
    "body",
    ["[poll]\nslow_every = 0\n", "[poll]\nfast_seconds = 0\n"],
)
def test_invalid_cadence_is_rejected(tmp_path, body) -> None:
    """slow_every = 0 would mean the status screen is never fetched."""
    with pytest.raises(ValidationError):
        load_settings(write(tmp_path, body))


def test_rollover_offset_is_configurable(tmp_path) -> None:
    """A receiver without the 1024-week bug sets this to 0."""
    cfg = write(tmp_path, "[display]\ngps_rollover_days = 0\n")
    assert load_settings(cfg).display.gps_rollover_days == 0
    assert Settings().display.gps_rollover_days == 7168
