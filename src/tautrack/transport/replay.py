"""Replay transport: serve recorded replies instead of talking to hardware (Q11).

Docker Desktop cannot pass a serial device through on Windows or macOS, and a
long-running capture may already hold the port. More importantly, replay is the only
way to get *deterministic* parser tests over the awkward real-world screen variants.

Fixtures are JSONL, one recorded transaction per line::

    {"command": "*IDN?", "body": "HEWLETT-PACKARD,Z3801A,...", "prompt": "scpi >"}
    {"command": ":SYNC:TINT?", "body": "", "prompt": "E-230>"}

Two behaviours here are deliberate rather than incidental:

* **Replies are fragmented across reads.** Real hardware delivered the status screen in
  six TCP segments, so serving it in one piece would let a buggy prompt scanner pass
  its tests and then fail against the device. ``chunk_size`` forces the same shape.
* **Unknown commands answer ``E-113>``**, which is what the firmware does for an
  undefined header. That keeps error handling on the tested path.
"""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from pathlib import Path

from .base import Transport, TransportError


class ReplayTransport(Transport):
    def __init__(
        self,
        path: Path,
        loop: bool = True,
        speed: float = 0.0,
        chunk_size: int = 180,
    ) -> None:
        self._path = Path(path)
        self._loop = loop
        self._speed = speed
        self._chunk_size = chunk_size
        self._by_command: dict[str, deque[tuple[str, str]]] = defaultdict(deque)
        self._originals: dict[str, list[tuple[str, str]]] = {}
        self._pending = bytearray()
        self._open = False

    @property
    def description(self) -> str:
        return f"replay:{self._path.name}"

    async def open(self) -> None:
        if not self._path.exists():
            raise TransportError(f"replay fixture not found: {self._path}")
        records = 0
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rec = json.loads(line)
            cmd = rec["command"].strip().upper()
            self._by_command[cmd].append((rec.get("body", ""), rec.get("prompt", "scpi >")))
            records += 1
        if records == 0:
            raise TransportError(f"replay fixture is empty: {self._path}")
        self._originals = {k: list(v) for k, v in self._by_command.items()}
        self._open = True

    async def close(self) -> None:
        self._open = False
        self._pending.clear()

    async def write(self, data: bytes) -> None:
        if not self._open:
            raise TransportError("replay transport not open")
        cmd = data.decode("ascii", errors="replace").strip().upper()
        if not cmd:
            return

        queue = self._by_command.get(cmd)
        if queue is not None and not queue and self._loop:
            # Exhausted: restart this command's recorded sequence so a long-running UI
            # keeps receiving data rather than stalling.
            queue.extend(self._originals[cmd])

        if queue:
            body, prompt = queue.popleft()
        elif cmd in ("*CLS", "*RST"):
            body, prompt = "", "scpi >"
        else:
            # Mirror the firmware: an undefined header yields E-113.
            body, prompt = "", "E-113>"

        reply = (body + "\r\n" if body else "") + prompt + " "
        self._pending.extend(reply.encode("ascii", errors="replace"))

        if self._speed > 0:
            await asyncio.sleep(self._speed)

    async def read(self, timeout: float) -> bytes:
        if not self._open:
            raise TransportError("replay transport not open")
        if not self._pending:
            # Nothing queued: behave like a quiet line rather than blocking forever.
            await asyncio.sleep(min(timeout, 0.01))
            return b""
        take = min(self._chunk_size, len(self._pending))
        chunk = bytes(self._pending[:take])
        del self._pending[:take]
        return chunk

    async def reset_input(self) -> None:
        self._pending.clear()
