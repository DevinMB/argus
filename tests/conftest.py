from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from argus.probes import Result

BASE = """
never_probe: [192.0.2.66]
checks:
  - {name: web, kind: http, target: "http://127.0.0.1:1/", site: B, interval: 60}
  - {name: db, kind: tcp, target: "127.0.0.1:1", site: A, severity: critical, interval: 30}
heartbeats:
  - {name: stack-sync, max_age: 900, site: B}
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "argus.yml"
    p.write_text(textwrap.dedent(BASE))
    return p


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeProber:
    """Returns scripted results per check name; defaults to success."""

    def __init__(self) -> None:
        self.script: dict[str, list[Result]] = {}
        self.calls: list[str] = []

    async def run(self, check) -> Result:
        self.calls.append(check.name)
        queue = self.script.get(check.name)
        return queue.pop(0) if queue else Result(ok=True, duration=0.01)

    async def aclose(self) -> None:
        pass


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def prober() -> FakeProber:
    return FakeProber()
