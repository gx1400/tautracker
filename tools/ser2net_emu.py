#!/usr/bin/env python3
"""A ser2net work-alike, for hosts where ser2net is not installed yet.

DEVELOPMENT TOOL, NOT PART OF THE PRODUCT. tautrack ships no bridge (decision Q12);
ser2net is the supported path. This exists so a container can reach a GPSDO before
ser2net can be installed - installing it needs elevation, which is not always to hand.

The point is **configuration compatibility**: write a real ser2net YAML config now, run
it with this, and when ser2net itself is installed swap the binary and change nothing
else. The accepted config subset and the CLI flags deliberately mirror ser2net 4.x.

    python tools/ser2net_emu.py -c ser2net.yaml -n -d

Supported config (ser2net 4.x YAML):

    connection: &gpsdo
      accepter: tcp,4196
      connector: serialdev,COM7,19200o71,local
      options:
        max-connections: 1
        kickolduser: false

Serial parameter strings follow ser2net's own notation: ``19200o71`` is 19200 baud,
odd parity, 7 data bits, 1 stop bit. ``n`` none, ``o`` odd, ``e`` even.

Deliberate deviations, all documented rather than silent:

* ``max-connections`` defaults to **1**, not ser2net's default. SCPI is request/response
  against shared device state, so a second reader consumes the first's replies (C-2).
* **RFC 2217** (telnet com-port control) is not implemented. tautrack never reconfigures
  the line remotely, so the raw ``tcp`` accepter is sufficient. A config asking for
  ``telnet`` is accepted and served as raw, with a warning.
* The **control port** (``-p``) is accepted and ignored.

One ser2net option is worth calling out because it will break SCPI silently: a
``banner`` is transmitted to the client on connect, so its bytes land at the head of
the first reply and corrupt the parse. It is implemented here for fidelity, but warned
about loudly. The same hazard applies to a hardware device server's heartbeat and
registration packets (see docs/scope.md).
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import re
import socketserver
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

try:
    import serial
except ImportError:  # pragma: no cover - a clear message beats a traceback
    print("pyserial is required: pip install pyserial", file=sys.stderr)
    raise SystemExit(2) from None

log = logging.getLogger("ser2net")

PARITY = {"n": "N", "o": "O", "e": "E"}
_SERIAL_SPEC = re.compile(r"^(?P<baud>\d+)(?P<parity>[noe])(?P<bits>[5-8])(?P<stop>[12])$")


@dataclass
class Connection:
    """One ser2net ``connection:`` entry."""

    name: str
    listen_host: str = "0.0.0.0"
    listen_port: int = 4196
    device: str = ""
    baudrate: int = 9600
    parity: str = "N"
    bytesize: int = 8
    stopbits: int = 1
    max_connections: int = 1
    banner: str | None = None
    kickolduser: bool = False
    telnet_requested: bool = False
    options: dict = field(default_factory=dict)

    @property
    def serial_spec(self) -> str:
        inv = {v: k for k, v in PARITY.items()}
        return f"{self.baudrate}{inv[self.parity]}{self.bytesize}{self.stopbits}"


def parse_serial_params(spec: str) -> dict:
    """Decode ser2net's ``19200o71`` notation.

    Getting this wrong is not academic: the Z3801A needs 7 data bits with odd parity,
    and a bridge that silently fell back to 8-N-1 would return garbage that looks like
    a dead instrument.
    """
    m = _SERIAL_SPEC.match(spec.strip())
    if not m:
        raise ValueError(
            f"cannot parse serial parameters {spec!r}; expected e.g. 19200o71 "
            f"(baud, parity n/o/e, data bits, stop bits)"
        )
    return {
        "baudrate": int(m.group("baud")),
        "parity": PARITY[m.group("parity")],
        "bytesize": int(m.group("bits")),
        "stopbits": int(m.group("stop")),
    }


def _split_csv(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def parse_connection(name: str, body: dict) -> Connection:
    conn = Connection(name=name)

    accepter = str(body.get("accepter", "")).strip()
    parts = _split_csv(accepter)
    if not parts:
        raise ValueError(f"connection {name!r}: missing 'accepter'")
    kind = parts[0].lower()
    if kind == "telnet":
        # ser2net's telnet accepter negotiates RFC 2217; served as raw here.
        conn.telnet_requested = True
        parts = parts[1:]
        kind = parts[0].lower() if parts else "tcp"
    if kind not in ("tcp", "tcp4", "tcp6"):
        raise ValueError(f"connection {name!r}: unsupported accepter {accepter!r}")
    # 'tcp,4196' or 'tcp,localhost,4196'
    addr = parts[1:]
    if len(addr) == 1:
        conn.listen_port = int(addr[0])
    elif len(addr) >= 2:
        conn.listen_host, conn.listen_port = addr[0], int(addr[1])
    else:
        raise ValueError(f"connection {name!r}: accepter has no port")

    connector = str(body.get("connector", "")).strip()
    cparts = _split_csv(connector)
    if not cparts or cparts[0].lower() not in ("serialdev", "serial"):
        raise ValueError(
            f"connection {name!r}: only a 'serialdev' connector is emulated, got {connector!r}"
        )
    if len(cparts) < 2:
        raise ValueError(f"connection {name!r}: connector has no device")
    conn.device = cparts[1]
    if len(cparts) >= 3:
        for key, value in parse_serial_params(cparts[2]).items():
            setattr(conn, key, value)
    # Trailing flags such as 'local' and 'nobreak' are accepted and ignored.

    opts = body.get("options") or {}
    conn.options = dict(opts)
    if "max-connections" in opts:
        conn.max_connections = int(opts["max-connections"])
    if opts.get("banner"):
        conn.banner = str(opts["banner"])
    conn.kickolduser = bool(opts.get("kickolduser", False))
    return conn


def load_config(path: Path) -> list[Connection]:
    """Read a ser2net 4.x YAML config.

    ser2net allows repeated ``connection:`` keys, which plain YAML does not, so files
    are read document-by-document and a ``connections:`` list is accepted too.
    """
    try:
        import yaml
    except ImportError:  # pragma: no cover
        print("PyYAML is required to read a ser2net config: pip install pyyaml", file=sys.stderr)
        raise SystemExit(2) from None

    text = path.read_text(encoding="utf-8")
    found: list[Connection] = []
    for doc in yaml.safe_load_all(text):
        if not isinstance(doc, dict):
            continue
        if isinstance(doc.get("connection"), dict):
            found.append(parse_connection(doc.get("name", f"con{len(found)}"), doc["connection"]))
        for item in doc.get("connections") or []:
            if isinstance(item, dict):
                name = item.get("name", f"con{len(found)}")
                found.append(parse_connection(name, item))
    if not found:
        raise ValueError(f"no connections found in {path}")
    return found


class _Handler(socketserver.BaseRequestHandler):
    """Pipes one TCP client to the serial port, and back."""

    def handle(self) -> None:
        srv: _Server = self.server  # type: ignore[assignment]
        conn = srv.conn
        peer = f"{self.client_address[0]}:{self.client_address[1]}"

        with srv.slots_lock:
            if srv.active >= conn.max_connections:
                if conn.kickolduser and srv.current is not None:
                    log.info("%s: kicking old user for %s", conn.name, peer)
                    srv.kick.set()
                else:
                    log.warning(
                        "%s: refusing %s - max-connections=%d reached",
                        conn.name,
                        peer,
                        conn.max_connections,
                    )
                    with contextlib.suppress(OSError):
                        self.request.sendall(
                            b"ser2net: too many connections, max-connections reached\r\n"
                        )
                    return
            srv.active += 1
            srv.current = peer

        log.info("%s: %s connected to %s (%s)", conn.name, peer, conn.device, conn.serial_spec)
        stop = threading.Event()
        try:
            if conn.banner:
                # Faithful to ser2net, and a genuine hazard: these bytes arrive before
                # the first reply and will corrupt a SCPI parse.
                self.request.sendall(conn.banner.encode("ascii", "replace"))

            # Discard anything buffered while nobody was listening, so this client's
            # first read begins at a reply boundary rather than mid-message. Safe here:
            # no reader thread is running for this client yet.
            srv.serial.reset_input_buffer()

            reader = threading.Thread(target=self._serial_to_client, args=(srv, stop), daemon=True)
            reader.start()

            self.request.settimeout(1.0)
            while not stop.is_set() and not srv.kick.is_set():
                try:
                    data = self.request.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                # No lock here. See _serial_to_client: the reader thread must not hold
                # a lock across its blocking read, or writes starve and commands never
                # reach the device.
                srv.serial.write(data)
        finally:
            stop.set()
            srv.kick.clear()
            with srv.slots_lock:
                srv.active = max(0, srv.active - 1)
                if srv.current == peer:
                    srv.current = None
            log.info("%s: %s disconnected", conn.name, peer)

    def _serial_to_client(self, srv: _Server, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                # Deliberately unlocked. Holding a lock across this blocking read, and
                # then re-acquiring it immediately, starves the writer: Python locks are
                # not fair, so queries stopped reaching the device and every reply timed
                # out with "0 read(s), 0 byte(s)". pyserial supports one reader thread
                # plus one writer thread, which is exactly this shape.
                data = srv.serial.read(4096)
            except Exception as exc:
                log.error("%s: serial read failed: %s", srv.conn.name, exc)
                stop.set()
                return
            if data:
                try:
                    self.request.sendall(data)
                except OSError:
                    stop.set()
                    return


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self.serial = serial.Serial(
            port=conn.device,
            baudrate=conn.baudrate,
            bytesize=conn.bytesize,
            parity=conn.parity,
            stopbits=conn.stopbits,
            timeout=0.2,
        )
        self.slots_lock = threading.Lock()
        self.active = 0
        self.current: str | None = None
        self.kick = threading.Event()
        super().__init__((conn.listen_host, conn.listen_port), _Handler)


def build_parser() -> argparse.ArgumentParser:
    # Flags mirror ser2net 4.x so an existing invocation works unchanged.
    p = argparse.ArgumentParser(
        prog="ser2net-emu",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-c", "--config-file", type=Path, help="ser2net YAML config")
    p.add_argument("-n", "--nodaemon", action="store_true", help="stay in the foreground")
    p.add_argument("-d", "--debug", action="store_true", help="verbose logging")
    p.add_argument("-p", "--control-port", help="accepted and ignored (not emulated)")
    p.add_argument("-v", "--version", action="store_true")
    # Convenience for a one-off with no config file at all.
    p.add_argument("--device", help="serial device, instead of a config file")
    p.add_argument("--port", type=int, default=4196, help="TCP port for --device")
    p.add_argument("--params", default="19200o71", help="serial params for --device")
    p.add_argument("--max-connections", type=int, default=1)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print("ser2net-emu (tautrack development tool; emulates ser2net 4.x config)")
        return 0

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s ser2net: %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.control_port:
        log.warning("control port %s requested but not emulated; ignoring", args.control_port)

    if args.config_file:
        connections = load_config(args.config_file)
    elif args.device:
        conn = Connection(name="cmdline", device=args.device, listen_port=args.port)
        for key, value in parse_serial_params(args.params).items():
            setattr(conn, key, value)
        conn.max_connections = args.max_connections
        connections = [conn]
    else:
        print("either -c CONFIG or --device is required", file=sys.stderr)
        return 2

    servers: list[_Server] = []
    for conn in connections:
        if conn.telnet_requested:
            log.warning(
                "%s: telnet accepter requested; RFC 2217 is not emulated, serving raw TCP",
                conn.name,
            )
        if conn.banner:
            log.warning(
                "%s: a banner is configured. Its bytes are sent before the first reply "
                "and WILL corrupt a SCPI parse - remove it unless the client expects one",
                conn.name,
            )
        try:
            srv = _Server(conn)
        except serial.SerialException as exc:
            log.error("%s: cannot open %s: %s", conn.name, conn.device, exc)
            return 1
        except OSError as exc:
            log.error(
                "%s: cannot listen on %s:%d: %s", conn.name, conn.listen_host, conn.listen_port, exc
            )
            return 1
        servers.append(srv)
        log.info(
            "%s: listening on %s:%d -> %s %s (max-connections=%d)",
            conn.name,
            conn.listen_host,
            conn.listen_port,
            conn.device,
            conn.serial_spec,
            conn.max_connections,
        )

    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
            s.serial.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
