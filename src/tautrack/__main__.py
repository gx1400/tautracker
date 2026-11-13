"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import sys
from pathlib import Path

import uvicorn

from . import __version__
from .config import Settings, load_settings
from .poller import Poller, housekeeping_loop
from .store.db import Store
from .web.app import create_app

log = logging.getLogger("tautrack")


def _build_parser() -> argparse.ArgumentParser:
    # Options shared by the top level and every subcommand, so both
    # "tautrack -c x.toml serve" and "tautrack serve -c x.toml" work. argparse
    # otherwise binds them to the parent only, and the second form fails.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="TOML config file (environment variables override it)",
    )
    common.add_argument("-v", "--verbose", action="store_true")

    p = argparse.ArgumentParser(prog="tautrack", description=__doc__, parents=[common])
    p.add_argument("--version", action="version", version=f"tautrack {__version__}")

    sub = p.add_subparsers(dest="command")
    sub.add_parser("serve", parents=[common], help="run the poller and web UI (default)")
    sub.add_parser("config", parents=[common], help="print the effective configuration")
    poll = sub.add_parser("poll-once", parents=[common], help="perform a single poll and print it")
    poll.add_argument("--no-store", action="store_true", help="do not write to the database")
    return p


async def _serve(settings: Settings) -> int:
    store = Store(settings.database)
    poller = Poller(settings, store)
    stop = asyncio.Event()

    app = create_app(settings, store, poller)
    config = uvicorn.Config(
        app,
        host=settings.web.host,
        port=settings.web.port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def _request_stop(*_: object) -> None:
        log.info("shutdown requested")
        stop.set()
        server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, _request_stop)

    # Each long-lived task is supervised: an unhandled exception in the poller must
    # surface rather than leaving a dead task and a UI that silently stops updating.
    tasks = [
        asyncio.create_task(poller.run(stop), name="poller"),
        asyncio.create_task(housekeeping_loop(settings, store, stop), name="housekeeping"),
        asyncio.create_task(server.serve(), name="web"),
    ]
    log.info(
        "tautrack %s serving on http://%s:%d (transport=%s)",
        __version__,
        settings.web.host,
        settings.web.port,
        settings.transport.kind,
    )
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            if (exc := t.exception()) is not None:
                log.error("task %s failed: %r", t.get_name(), exc)
    finally:
        stop.set()
        server.should_exit = True
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()
    return 0


async def _poll_once(settings: Settings, no_store: bool) -> int:
    store = Store(settings.database if not no_store else Path(":memory:"))
    poller = Poller(settings, store)
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    try:
        for _ in range(100):
            await asyncio.sleep(0.25)
            if poller.last_poll_ts:
                break
        if poller.device_id is None:
            print("no device identified", file=sys.stderr)
            return 1
        latest = store.latest(poller.device_id)
        for name in sorted(latest):
            entry = latest[name]
            value = entry["text"] if entry["text"] is not None else entry["value"]
            print(f"{name:32s} {value}")
        sats = store.latest_satellites(poller.device_id)
        if sats:
            print(f"\n{len(sats)} satellite(s):")
            for s in sats:
                mark = "T" if s["tracked"] else ("a" if s["acquiring"] else " ")
                print(
                    f"  [{mark}] PRN {s['prn']:>2}  el {s['elevation']}  "
                    f"az {s['azimuth']}  ss {s['signal']}"
                )
    finally:
        stop.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = load_settings(args.config)

    command = args.command or "serve"
    if command == "config":
        print(settings.model_dump_json(indent=2))
        return 0
    if command == "poll-once":
        return asyncio.run(_poll_once(settings, args.no_store))
    return asyncio.run(_serve(settings))


if __name__ == "__main__":
    raise SystemExit(main())
