import asyncio

from fleet.egress import start


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    writer.write(await reader.read(100))
    await writer.drain()
    writer.close()


async def _through(proxy_port: int, request: bytes) -> tuple[bytes, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(request)
    await writer.drain()
    status = await reader.readuntil(b"\r\n\r\n")
    echoed = b""
    if status.startswith(b"HTTP/1.1 200"):
        writer.write(b"ping")
        await writer.drain()
        echoed = await reader.read(100)
    writer.close()
    return status.split(b"\r\n", 1)[0], echoed


async def test_the_proxy_forwards_exactly_the_granted_destinations() -> None:
    target = await asyncio.start_server(_echo, "127.0.0.1", 0)
    target_port = target.sockets[0].getsockname()[1]
    granted = f"127.0.0.1:{target_port}"
    proxy = await start(0, frozenset({granted}), host="127.0.0.1")
    proxy_port = proxy.sockets[0].getsockname()[1]
    async with target, proxy:
        allowed = await _through(proxy_port, f"CONNECT {granted} HTTP/1.1\r\n\r\n".encode())
        assert allowed == (b"HTTP/1.1 200 Connection Established", b"ping")
        other = f"CONNECT 127.0.0.1:{target_port + 1} HTTP/1.1\r\n\r\n".encode()
        assert (await _through(proxy_port, other))[0] == b"HTTP/1.1 403 Forbidden"
        plain = f"GET http://{granted}/ HTTP/1.1\r\n\r\n".encode()
        assert (await _through(proxy_port, plain))[0] == b"HTTP/1.1 405 Method Not Allowed"


async def test_a_granted_destination_that_is_down_gets_a_bad_gateway() -> None:
    closed = await asyncio.start_server(_echo, "127.0.0.1", 0)
    port = closed.sockets[0].getsockname()[1]
    closed.close()
    await closed.wait_closed()
    proxy = await start(0, frozenset({f"127.0.0.1:{port}"}), host="127.0.0.1")
    async with proxy:
        request = f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode()
        assert (await _through(proxy.sockets[0].getsockname()[1], request))[0] == (
            b"HTTP/1.1 502 Bad Gateway"
        )
