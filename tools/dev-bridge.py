#!/usr/bin/env python3
"""Development-only serial-to-TCP shim.

NOT PART OF THE PRODUCT. tautrack ships no bridge (decision Q12) - ser2net is the
supported path on hosts where Docker cannot pass a serial device through. This exists
so a container can reach a GPSDO on a Windows or macOS development host before
ser2net is installed.

Deliberate design limits, both following from the SCPI session model (constraint C-2):

  * ONE client at a time. SCPI is request/response against shared device state, so
    concurrent readers would interleave and corrupt each other's replies. A second
    connection is refused with a message rather than silently accepted.
  * No framing, no heartbeat, no registration packets. Anything injected into a
    transparent serial stream lands inside a reply and corrupts the parse - the same
    trap documented for hardware device servers in docs/scope.md.

Binds 0.0.0.0 by default so a container can reach it. On Docker Desktop connect to
``host.docker.internal:<port>``. A host firewall may need to allow the listener.

Usage:
    python tools/dev-bridge.py --serial COM7 --listen 4196
    python tools/dev-bridge.py --serial /dev/ttyUSB0 --listen 4196
"""

from __future__ import annotations

import argparse
import contextlib
import socket
import threading
import time

import serial

PARITY = {"none": serial.PARITY_NONE, "odd": serial.PARITY_ODD, "even": serial.PARITY_EVEN}
BYTESIZE = {5: serial.FIVEBITS, 6: serial.SIXBITS, 7: serial.SEVENBITS, 8: serial.EIGHTBITS}
STOPBITS = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def pump_serial_to_client(ser: serial.Serial, conn: socket.socket, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            data = ser.read(4096)
        except Exception as exc:
            log(f"serial read failed: {exc}")
            stop.set()
            return
        if data:
            try:
                conn.sendall(data)
            except OSError:
                stop.set()
                return


def serve(ser: serial.Serial, listen_host: str, listen_port: int) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((listen_host, listen_port))
    srv.listen(2)  # backlog of 2 so a second attempt is accepted purely to be refused
    log(f"listening on {listen_host}:{listen_port} (one client at a time)")

    busy = False
    while True:
        conn, addr = srv.accept()
        if busy:
            # Refuse rather than accept: two SCPI readers would corrupt each other.
            log(f"refusing {addr[0]}:{addr[1]} - a client is already connected")
            with contextlib.suppress(OSError):
                conn.sendall(b"tautrack dev-bridge: busy, one client at a time\r\n")
            conn.close()
            continue

        busy = True
        log(f"client {addr[0]}:{addr[1]} connected")
        # Discard anything the device emitted while nobody was listening, so the new
        # client's first read starts at a clean boundary rather than mid-reply.
        with contextlib.suppress(Exception):
            ser.reset_input_buffer()

        stop = threading.Event()
        pump = threading.Thread(target=pump_serial_to_client, args=(ser, conn, stop), daemon=True)
        pump.start()
        try:
            conn.settimeout(1.0)
            while not stop.is_set():
                try:
                    data = conn.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                ser.write(data)
        finally:
            stop.set()
            pump.join(timeout=2)
            conn.close()
            busy = False
            log("client disconnected")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--serial", required=True, help="serial device, e.g. COM7 or /dev/ttyUSB0")
    ap.add_argument("--listen", type=int, default=4196, help="TCP port to listen on (default 4196)")
    ap.add_argument(
        "--bind",
        default="0.0.0.0",
        help="address to bind (default 0.0.0.0, reachable from containers)",
    )
    ap.add_argument("--baud", type=int, default=19200)
    ap.add_argument("--bits", type=int, default=7, choices=sorted(BYTESIZE))
    ap.add_argument("--parity", default="odd", choices=sorted(PARITY))
    ap.add_argument("--stopbits", type=int, default=1, choices=sorted(STOPBITS))
    args = ap.parse_args()

    ser = serial.Serial(
        port=args.serial,
        baudrate=args.baud,
        bytesize=BYTESIZE[args.bits],
        parity=PARITY[args.parity],
        stopbits=STOPBITS[args.stopbits],
        timeout=0.2,
    )
    log(
        f"serial open: {args.serial} {args.baud} {args.bits}{args.parity[0].upper()}{args.stopbits}"
    )
    try:
        serve(ser, args.bind, args.listen)
    except KeyboardInterrupt:
        log("stopping")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
