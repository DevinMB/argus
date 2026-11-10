import asyncio

import httpx

from argus import sniff
from argus.start import suggestions


def answers(table):
    """A fake network: target -> (scheme, status), anything else refuses."""
    def handle(req):
        hit = table.get(f"{req.url.host}:{req.url.port}")
        if hit and hit[0] == req.url.scheme:
            return httpx.Response(hit[1])
        raise httpx.ConnectError("refused")
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def host(containers):
    return [{"name": "lab", "address": "192.168.1.250", "status": "ok",
             "report": {"containers": containers}}]


def test_a_port_that_speaks_http_becomes_an_http_check():
    s = sniff.Sniffer(clock=lambda: 1000.0)
    s.client = answers({"192.168.1.250:8420": ("http", 200), "192.168.1.250:3000": ("http", 302),
                        "192.168.1.250:8006": ("https", 401)})
    hosts = host([
        {"name": "atlas", "image": "ghcr.io/x/atlas", "state": "running", "ports": [8420]},
        {"name": "grafana", "image": "grafana/grafana", "state": "running", "ports": [3000]},
        {"name": "pve-ui", "image": "x", "state": "running", "ports": [8006]},
        {"name": "couchdb", "image": "couchdb:3", "state": "running", "ports": [5432]},
        {"name": "node-exporter", "image": "prom/node-exporter", "state": "running",
         "ports": [9100]}])

    async def go():
        first = suggestions(hosts, [], s)  # nothing known yet: TCP, and a look starts
        assert {x["check"]["kind"] for x in first} == {"tcp"}
        await s.task
        return suggestions(hosts, [], s)

    by = {x["container"]: x for x in asyncio.run(go())}
    assert by["atlas"]["check"]["kind"] == "http"
    assert by["atlas"]["check"]["target"] == "http://192.168.1.250:8420/"
    assert "expect_status" not in by["atlas"]["check"]
    assert by["grafana"]["check"]["expect_status"] == [302]  # a login redirect is the app answering
    assert by["pve-ui"]["check"]["target"].startswith("https://")
    assert by["pve-ui"]["check"]["verify_tls"] is False
    assert by["couchdb"]["check"]["kind"] == "tcp"  # answered nothing HTTP
    assert by["node-exporter"]["internal"] and not by["atlas"]["internal"]
    assert [x["container"] for x in asyncio.run(_sorted(hosts, s))][-1] == "node-exporter"


async def _sorted(hosts, s):
    return suggestions(hosts, [], s)


def test_ports_only_the_companion_reaches_stay_tcp():
    check = {"name": "db", "kind": "tcp", "target": "127.0.0.1:5432", "companion": "lab"}
    assert sniff.shape(check, {"scheme": "http", "status": 200, "at": 0}) == check
