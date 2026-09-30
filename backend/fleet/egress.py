"""The egress proxy: a task reaches exactly the destinations its batch was granted, nothing else.

It speaks only HTTP CONNECT, the way HTTPS clients tunnel through a proxy. A CONNECT to a granted
host:port is spliced through; any other destination gets a 403, and anything but CONNECT a 405.
"""

import argparse
import asyncio
import contextlib
import logging
import sys

logger = logging.getLogger(__name__)
_HEAD_LIMIT = 8192


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            writer.close()


async def _refuse(writer: asyncio.StreamWriter, status: bytes) -> None:
    writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Length: 0\r\n\r\n")
    with contextlib.suppress(OSError):
        await writer.drain()
        writer.close()


async def handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, allowed: frozenset[str]
) -> None:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
        writer.close()
        return
    parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
    if len(parts) != 3 or parts[0] != "CONNECT":
        await _refuse(writer, b"405 Method Not Allowed")
        return
    target = parts[1].lower()
    if target not in allowed:
        logger.info("refused CONNECT to %s", target)
        await _refuse(writer, b"403 Forbidden")
        return
    host, _, port = target.rpartition(":")
    try:
        upstream_reader, upstream_writer = await asyncio.wait_for(
            asyncio.open_connection(host, int(port)), timeout=10
        )
    except (OSError, TimeoutError):
        await _refuse(writer, b"502 Bad Gateway")
        return
    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))


async def start(port: int, allowed: frozenset[str], host: str = "0.0.0.0") -> asyncio.Server:
    return await asyncio.start_server(
        lambda reader, writer: handle(reader, writer, allowed), host, port, limit=_HEAD_LIMIT
    )


async def serve(port: int, allowed: frozenset[str]) -> None:
    async with await start(port, allowed) as server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3128)
    parser.add_argument("--allow", action="append", default=[], help="a granted host:port")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    asyncio.run(serve(args.port, frozenset(item.lower() for item in args.allow)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
