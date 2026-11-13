"""Transports: serial, TCP and replay, behind one narrow interface."""
from __future__ import annotations

from ..config import TransportSettings
from .base import PROMPT_ANY, Reply, Transport, TransportError
from .replay import ReplayTransport
from .serial_transport import SerialTransport
from .tcp import TcpTransport

__all__ = [
    "PROMPT_ANY",
    "Reply",
    "ReplayTransport",
    "SerialTransport",
    "TcpTransport",
    "Transport",
    "TransportError",
    "build_transport",
]


def build_transport(cfg: TransportSettings) -> Transport:
    """Construct the configured transport.

    Everything above this function is transport-agnostic, which is what lets replay
    substitute for hardware without special cases elsewhere.
    """
    if cfg.kind == "serial":
        s = cfg.serial
        return SerialTransport(
            port=s.port,
            baudrate=s.baudrate,
            bytesize=s.bytesize,
            parity=s.parity,
            stopbits=s.stopbits,
        )
    if cfg.kind == "tcp":
        return TcpTransport(host=cfg.tcp.host, port=cfg.tcp.port)
    if cfg.kind == "replay":
        r = cfg.replay
        return ReplayTransport(path=r.path, loop=r.loop, speed=r.speed)
    raise ValueError(f"unknown transport kind: {cfg.kind}")
