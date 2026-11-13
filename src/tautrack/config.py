"""Configuration: TOML file plus ``TAUTRACK_``-prefixed environment overrides.

Decision Q8. `tomllib` is in the standard library from 3.11, so file parsing costs no
dependency. Nested values are overridden with ``__``, the Pydantic Settings convention:

    TAUTRACK_TRANSPORT__PORT=/dev/ttyUSB0
    TAUTRACK_POLL__FAST_SECONDS=10

Precedence is environment > file > defaults.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class SerialSettings(BaseModel):
    """Serial line parameters.

    The defaults are the HP Z3801A's, which are unusual: 7 data bits with odd parity,
    where most equipment is 8-N-1. Device servers that only offer 8-N-1 cannot talk to
    these units at all.
    """

    port: str = "COM7"
    baudrate: int = 19200
    bytesize: Literal[5, 6, 7, 8] = 7
    parity: Literal["none", "odd", "even"] = "odd"
    stopbits: Literal[1, 2] = 1


class TcpSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 4196


class ReplaySettings(BaseModel):
    """Replay a recorded session (Q11), for development and CI with no hardware."""

    path: Path = Path("tests/fixtures/session.jsonl")
    #: Replay in a loop so a long-running UI keeps receiving data.
    loop: bool = True
    #: Compress recorded inter-poll delays; 0 disables waiting entirely.
    speed: float = 0.0


class TransportSettings(BaseModel):
    kind: Literal["serial", "tcp", "replay"] = "replay"
    serial: SerialSettings = Field(default_factory=SerialSettings)
    tcp: TcpSettings = Field(default_factory=TcpSettings)
    replay: ReplaySettings = Field(default_factory=ReplaySettings)
    #: Seconds to wait for a SCPI prompt before treating a query as failed. The status
    #: screen measured 2.84 s on real hardware, so this leaves generous headroom.
    read_timeout: float = 8.0
    #: Backoff bounds for reconnection (FR-6).
    reconnect_min: float = 1.0
    reconnect_max: float = 60.0


class PollSettings(BaseModel):
    """Split cadence (C-1).

    Discrete queries measured ~0.20 s each and the status screen ~2.84 s, so a fast
    tick of nine short queries costs under two seconds and fits comfortably in 15 s.
    """

    fast_seconds: float = 15.0
    #: Fetch the full status screen every Nth fast tick.
    slow_every: int = 4


class RetentionSettings(BaseModel):
    """Zabbix-style history/trends tiering (Q3).

    Aggregates keep min and max, not just mean, so a ten-minute holdover stays visible
    in a one-year view (FR-10).
    """

    raw_days: int = 14
    hourly_days: int = 400
    daily_days: int = 3650
    #: How often the rollup/prune task runs.
    housekeeping_minutes: int = 30


class OtelSettings(BaseModel):
    enabled: bool = False
    endpoint: str = "http://localhost:4318"
    service_name: str = "tautrack"
    #: Export metrics, logs and traces independently (FR-20).
    metrics: bool = True
    logs: bool = True
    traces: bool = True


class WebSettings(BaseModel):
    """HTTP listener.

    Defaults to loopback deliberately: there is no authentication in v1 (NFR-6), so
    binding every interface must be a conscious choice rather than something a user
    gets by accident. Set it explicitly to expose the UI:

        [web]
        host = "0.0.0.0"

    or ``TAUTRACK_WEB__HOST=0.0.0.0``. In a container the listener must bind 0.0.0.0
    to be reachable through a published port at all - that is the normal case, and the
    container's network boundary is what limits exposure.
    """

    host: str = "127.0.0.1"
    port: int = 8080


class DisplaySettings(BaseModel):
    """Clock display (FR-18).

    1996 firmware reports a date 1024 weeks behind actual, a GPS week-number rollover.
    The offset corrects the display only; stored values stay exactly as reported.
    """

    timezone: str = "UTC"
    #: 1024 GPS weeks = 7168 days. Set to 0 for a receiver without the rollover bug.
    gps_rollover_days: int = 7168


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TAUTRACK_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    database: Path = Path("data/tautrack.db")
    transport: TransportSettings = Field(default_factory=TransportSettings)
    poll: PollSettings = Field(default_factory=PollSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    otel: OtelSettings = Field(default_factory=OtelSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    display: DisplaySettings = Field(default_factory=DisplaySettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Put the environment ahead of init kwargs.

        The TOML file is supplied as init kwargs, and pydantic-settings ranks those
        highest by default - which would make the file beat the environment, the
        opposite of the documented precedence. Containers in particular need the
        environment to win, so a mounted config can be overridden per deployment.

        Resulting order: environment > file > defaults.
        """
        return (env_settings, dotenv_settings, init_settings, file_secret_settings)

    @model_validator(mode="after")
    def _check_cadence(self) -> Settings:
        if self.poll.slow_every < 1:
            raise ValueError("poll.slow_every must be >= 1")
        if self.poll.fast_seconds <= 0:
            raise ValueError("poll.fast_seconds must be > 0")
        return self


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from TOML, then let the environment override.

    Pydantic Settings applies environment variables on top of whatever is passed in,
    which gives the required environment > file > defaults precedence for free.
    """
    data: dict = {}
    if path is not None and path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    return Settings(**data)
