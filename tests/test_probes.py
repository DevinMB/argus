import asyncio
import shutil
import socket

import pytest

from argus.config import Check
from argus.probes import Prober


async def http_server(status_line: str, delay: float = 0):
    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(delay)
        writer.write(f"HTTP/1.1 {status_line}\r\nContent-Length: 0\r\n"
                     "Location: /login\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def http(port, **kw):
    return Check(name="x", kind="http", target=f"http://127.0.0.1:{port}/", site="B", **kw)


@pytest.fixture
async def prober():
    p = Prober()
    yield p
    await p.aclose()


@pytest.mark.parametrize("status, kw, ok, error", [
    ("200 OK", {}, True, ""),
    ("500 Internal Server Error", {}, False, "HTTP 500, expected 200"),
    ("404 Not Found", {"expect_status": [404]}, True, ""),
    ("302 Found", {}, False, "HTTP 302, expected 200"),  # redirects are not followed
])
async def test_http(prober, status, kw, ok, error):
    server, port = await http_server(status)
    async with server:
        r = await prober.run(http(port, **kw))
    assert (r.ok, r.error) == (ok, error)


async def test_http_timeout(prober):
    server, port = await http_server("200 OK", delay=2)
    async with server:
        r = await prober.run(http(port, timeout=0.3))
    assert not r.ok and r.error == "timed out after 0.3s"


async def test_http_refused(prober):
    r = await prober.run(http(free_port()))
    assert not r.ok and "ConnectError" in r.error


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_tcp_open_and_closed(prober):
    server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert (await prober.run(Check(name="x", kind="tcp", target=f"127.0.0.1:{port}",
                                       site="B"))).ok
    r = await prober.run(Check(name="x", kind="tcp", target=f"127.0.0.1:{free_port()}", site="B"))
    assert not r.ok and "ConnectionRefusedError" in r.error


@pytest.mark.skipif(not shutil.which("ping"), reason="no ping binary")
async def test_ping_loopback(prober):
    r = await prober.run(Check(name="x", kind="ping", target="127.0.0.1", site="B", timeout=2))
    if not r.ok and "permitted" in r.error:
        pytest.skip("ICMP not permitted in this environment")
    assert r.ok, r.error


async def test_ping_failure_is_a_result_not_an_exception(prober):
    r = await prober.run(Check(name="x", kind="ping", target="no-such-host.invalid", site="B",
                               timeout=1))
    assert not r.ok and r.error
