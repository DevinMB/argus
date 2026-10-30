"""The three probe kinds. Each returns a Result and never raises."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass

import httpx

from argus.config import Check


@dataclass(frozen=True)
class Result:
    ok: bool
    duration: float
    error: str = ""


def _err(e: BaseException) -> str:
    text = str(e).strip()
    name = type(e).__name__
    return f"{name}: {text}"[:300] if text else name


class Prober:
    """Holds the two HTTP clients (TLS verified and not) for the process lifetime."""

    def __init__(self) -> None:
        headers = {"User-Agent": "argus"}
        self._verified = httpx.AsyncClient(verify=True, headers=headers)
        self._unverified = httpx.AsyncClient(verify=False, headers=headers)

    async def aclose(self) -> None:
        await self._verified.aclose()
        await self._unverified.aclose()

    async def run(self, check: Check) -> Result:
        start = time.monotonic()
        try:
            if check.kind == "http":
                error = await self._http(check)
            elif check.kind == "tcp":
                error = await _tcp(check)
            else:
                error = await _ping(check)
        except Exception as e:  # a probe must never take the scheduler down
            error = _err(e)
        return Result(ok=not error, duration=time.monotonic() - start, error=error)

    async def _http(self, check: Check) -> str:
        client = self._verified if check.verify_tls else self._unverified
        try:
            # Redirects are not followed: a login page answering 302 is a
            # different thing from the service answering 200.
            resp = await client.get(check.target, timeout=check.timeout)
        except httpx.TimeoutException:
            return f"timed out after {check.timeout:g}s"
        except httpx.HTTPError as e:
            return _err(e)
        if resp.status_code not in check.expect_status:
            want = "/".join(map(str, check.expect_status))
            return f"HTTP {resp.status_code}, expected {want}"
        return ""


async def _tcp(check: Check) -> str:
    host, _, port = check.target.rpartition(":")
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host.strip("[]"), int(port)), check.timeout
        )
    except TimeoutError:
        return f"timed out after {check.timeout:g}s"
    except OSError as e:
        return _err(e)
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return ""


async def _ping(check: Check) -> str:
    # iputils ping: -c 1 one echo, -W whole seconds to wait for the reply.
    proc = await asyncio.create_subprocess_exec(
        "ping", "-n", "-q", "-c", "1", "-W", str(max(1, math.ceil(check.timeout))), check.target,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), check.timeout + 2)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return f"timed out after {check.timeout:g}s"
    if proc.returncode == 0:
        return ""
    detail = stderr.decode(errors="replace").strip().splitlines()
    return detail[-1][:300] if detail else "no reply"
