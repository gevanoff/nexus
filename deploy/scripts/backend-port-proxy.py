#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import signal
import sys
from collections.abc import Sequence


@dataclasses.dataclass(frozen=True)
class Forward:
    name: str
    listen_host: str
    listen_port: int
    target_host: str
    target_port: int


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def parse_endpoint(raw: str, label: str) -> tuple[str, int]:
    if ":" not in raw:
        raise argparse.ArgumentTypeError(f"{label} endpoint must be host:port")
    host, port_raw = raw.rsplit(":", 1)
    if not host:
        raise argparse.ArgumentTypeError(f"{label} endpoint host must not be empty")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} endpoint port must be an integer") from exc
    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError(f"{label} endpoint port must be between 1 and 65535")
    return host, port


def parse_forward(raw: str) -> Forward:
    try:
        name, listen_raw, target_raw = raw.split("=", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "forward must use NAME=LISTEN_HOST:LISTEN_PORT=TARGET_HOST:TARGET_PORT"
        ) from exc
    if not name:
        raise argparse.ArgumentTypeError("forward name must not be empty")
    listen_host, listen_port = parse_endpoint(listen_raw, "listen")
    target_host, target_port = parse_endpoint(target_raw, "target")
    return Forward(
        name=name,
        listen_host=listen_host,
        listen_port=listen_port,
        target_host=target_host,
        target_port=target_port,
    )


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def pipe(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        raise
    finally:
        await close_writer(writer)


async def handle_client(
    forward: Forward,
    connect_timeout: float,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    peer = client_writer.get_extra_info("peername")
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(forward.target_host, forward.target_port),
            timeout=connect_timeout,
        )
    except Exception as exc:
        log(f"{forward.name}: upstream connect failed for {peer}: {type(exc).__name__}: {exc}")
        await close_writer(client_writer)
        return

    tasks = [
        asyncio.create_task(pipe(client_reader, upstream_writer)),
        asyncio.create_task(pipe(upstream_reader, client_writer)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(*tasks)


async def run(forwards: Sequence[Forward], connect_timeout: float) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    servers: list[asyncio.AbstractServer] = []
    try:
        for forward in forwards:
            server = await asyncio.start_server(
                lambda reader, writer, item=forward: handle_client(item, connect_timeout, reader, writer),
                host=forward.listen_host,
                port=forward.listen_port,
                reuse_address=True,
            )
            servers.append(server)
            log(
                f"{forward.name}: listening on {forward.listen_host}:{forward.listen_port} "
                f"-> {forward.target_host}:{forward.target_port}"
            )
        await stop.wait()
    finally:
        for server in servers:
            server.close()
        await asyncio.gather(*(server.wait_closed() for server in servers), return_exceptions=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Small TCP forwarder for host-side Nexus backend proxies.")
    parser.add_argument(
        "--forward",
        action="append",
        type=parse_forward,
        default=[],
        metavar="SPEC",
        help="Forward spec: NAME=LISTEN_HOST:LISTEN_PORT=TARGET_HOST:TARGET_PORT",
    )
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--check", action="store_true", help="Validate arguments and exit without listening.")
    args = parser.parse_args(argv)

    if not args.forward:
        parser.error("at least one --forward is required")
    if args.check:
        return 0
    asyncio.run(run(args.forward, args.connect_timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
