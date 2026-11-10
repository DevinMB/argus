import re

import pytest
from pydantic import ValidationError

from argus.config import Config, load


def checks(*items, **extra):
    return Config.model_validate({"checks": list(items), **extra})


def test_loads_with_defaults(config_file):
    cfg, digest = load(config_file)
    web = cfg.checks[0]
    assert (web.severity, web.timeout, web.expect_status, web.verify_tls) == (
        "warning", 5, (200,), True)
    assert cfg.heartbeats[0].max_age == 900
    assert len(digest) == 12


def test_empty_file_is_an_empty_config(tmp_path):
    p = tmp_path / "argus.yml"
    p.write_text("")
    assert load(p)[0].checks == ()


@pytest.mark.parametrize("bad, why", [
    ({"name": "x", "kind": "http", "target": "http://h/", "site": "B", "tagret": 1}, "extra"),
    ({"name": "X", "kind": "http", "target": "http://h/", "site": "B"}, "pattern"),
    ({"name": "x", "kind": "smtp", "target": "h:25", "site": "B"}, "kind"),
    ({"name": "x", "kind": "http", "target": "h:80", "site": "B"}, "http(s) URL"),
    ({"name": "x", "kind": "tcp", "target": "h", "site": "B"}, "host:port"),
    ({"name": "x", "kind": "tcp", "target": "h:70000", "site": "B"}, "host:port"),
    ({"name": "x", "kind": "ping", "target": "h:22", "site": "B"}, "bare host"),
    ({"name": "x", "kind": "ping", "target": "h", "site": "-c"}, "site"),
    ({"name": "x", "kind": "ping", "target": "h", "site": "B", "timeout": 60, "interval": 30},
     "shorter than interval"),
])
def test_rejects_malformed_checks(bad, why):
    with pytest.raises(ValidationError, match=re.escape(why)):
        checks(bad)


def test_rejects_duplicate_names_across_checks_and_heartbeats():
    with pytest.raises(ValidationError, match="duplicate names: a"):
        Config.model_validate({
            "checks": [{"name": "a", "kind": "ping", "target": "h", "site": "B"}],
            "heartbeats": [{"name": "a", "max_age": 60, "site": "B"}],
        })


@pytest.mark.parametrize("check", [
    {"name": "x", "kind": "ping", "target": "192.0.2.66", "site": "A"},
    {"name": "x", "kind": "tcp", "target": "192.0.2.66:22", "site": "A"},
    {"name": "x", "kind": "http", "target": "https://192.0.2.66:8006/", "site": "A"},
])
def test_never_probe_blocks_every_kind(check):
    with pytest.raises(ValidationError, match="never_probe"):
        checks(check, never_probe=["192.0.2.66"])


def test_never_probe_blocks_a_name_that_resolves_to_it():
    with pytest.raises(ValidationError, match="never_probe"):
        checks({"name": "x", "kind": "ping", "target": "localhost", "site": "A"},
               never_probe=["127.0.0.1"])


def test_group_descriptions_keep_order_and_must_be_used():
    cfg = checks({"name": "x", "kind": "ping", "target": "h", "site": "B", "group": "b"},
                 {"name": "y", "kind": "ping", "target": "h", "site": "B", "group": "a"},
                 groups={"b": "bees", "a": "ants"})
    assert list(cfg.groups) == ["b", "a"]
    with pytest.raises(ValidationError, match="groups with no checks: z"):
        checks({"name": "x", "kind": "ping", "target": "h", "site": "B"}, groups={"z": "zebras"})


def test_sites_and_links():
    cfg = Config.model_validate({
        "sites": {"B": "Home", "A": "Remote"},
        "links": [{"label": "Alerts", "url": "http://192.0.2.20:9090/alerts", "group": "Alerts"}],
    })
    assert list(cfg.sites) == ["B", "A"] and cfg.links[0].group == "Alerts"
    with pytest.raises(ValidationError, match="url must be http"):
        Config.model_validate({"links": [{"label": "x", "url": "ftp://192.0.2.1/"}]})
    with pytest.raises(ValidationError):
        Config.model_validate({"sites": {"a b": "Elsewhere"}})


def test_any_number_of_sites():
    ping = lambda n, **kw: {"name": n, "kind": "ping", "target": "h", **kw}  # noqa: E731
    cfg = Config.model_validate({
        "sites": {"home": "Home", "office": "Office", "cabin": "Cabin", "dc-1": "Rack"},
        "checks": [ping("a"), ping("b", site="cabin"), ping("c", site="dc-1")],
        "heartbeats": [{"name": "d", "max_age": 60, "site": "office"}],
    })
    assert [c.site for c in cfg.checks] == ["home", "cabin", "dc-1"]
    assert cfg.heartbeats[0].site == "office"
    with pytest.raises(ValidationError, match="unknown site attic"):
        Config.model_validate({"sites": {"home": "Home"}, "checks": [ping("a", site="attic")]})


def test_sites_default_to_the_ones_used():
    ping = lambda n, **kw: {"name": n, "kind": "ping", "target": "h", **kw}  # noqa: E731
    assert Config.model_validate({}).sites == {"home": "home"}
    one = Config.model_validate({"checks": [ping("a"), ping("b")]})
    assert one.sites == {"home": "home"} and {c.site for c in one.checks} == {"home"}
    two = Config.model_validate({"checks": [ping("a", site="B"), ping("b", site="A"), ping("c")]})
    assert list(two.sites) == ["B", "A", "home"]
