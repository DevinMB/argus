# argus.yml

Everything Argus watches is one YAML file. Most people never open it: **Add a check** and the Config page edit it for you, and keep its comments and layout. It can live in Argus, in a GitHub repo, or as a mounted file ([guide](guide.md#config)). Argus checks every change before using it; a file with a mistake is refused and the running config stays.

```yaml
checks:
  - {name: jellyfin, kind: http, target: "http://nas.lan:8096/health", severity: critical}
  - {name: db, kind: tcp, target: "nas.lan:5432", group: data}
  - {name: printer, kind: ping, target: printer.lan, interval: 300}
  - {name: grafana, kind: http, target: "http://127.0.0.1:3000/", companion: nas}
heartbeats:
  - {name: photo-sync, max_age: 3600}
jobs:
  - {name: nas-backup, max_age: 691200, schedule: "Sundays 02:00", severity: critical}
```

Names are lowercase letters, digits and dashes, and unique across checks, heartbeats and jobs.

## checks

| Field | Default | |
| --- | --- | --- |
| `name` | | required |
| `kind` | | `http`, `tcp` or `ping` |
| `target` | | `http`: an http(s) URL; `tcp`: `host:port`; `ping`: a host |
| `severity` | `warning` | `critical` pages; `warning` shows on the dashboard |
| `interval` | `60` | seconds between probes (10 to 3600) |
| `timeout` | `5` | seconds; shorter than the interval |
| `expect_status` | `[200]` | `http`: the status codes that count as up |
| `verify_tls` | `true` | `http`: check the certificate |
| `group` | `default` | groups the dashboard by what things are |
| `site` | the first site | see `sites` |
| `companion` | | run the check from inside this host, for things Argus cannot reach |
| `description` | | a note shown with the check |

A check is down after three failures in a row, and up again after one success.

## heartbeats

Something that runs on its own (a timer, a cron job) says "I ran" with `POST /heartbeat/<name>`. It is late after `max_age` seconds without one.

| Field | Default | |
| --- | --- | --- |
| `name` | | required |
| `max_age` | | seconds, at least 60 |
| `severity`, `group`, `site`, `description` | | as for checks |

A timer that deploys an app can add `?version=<commit>` to say what runs now.

## jobs

A scheduled run (a backup, a restore test) reports how it went when it ends:

```bash
curl -fsS -X POST https://argus:8443/job/nas-backup -H 'content-type: application/json' \
  -d '{"result": "ok", "started": 1793300000, "finished": 1793301500, "summary": "412 GB sent"}'
```

`result` is `ok` or `failed`; `summary` is one line; `items` can list what it covered (`[{"name": "photos", "result": "ok"}]`); `log` keeps the tail of its output (64 KB). A failed run, or none within `max_age`, is a problem. Jobs are also what patching reads to know a host's backup is fresh and has passed a restore test.

| Field | Default | |
| --- | --- | --- |
| `name` | | required |
| `max_age` | | seconds without a run before it is late |
| `schedule` | | when it runs, for people ("Sundays 02:00"); the week view reads it |
| `title`, `steps` | | what it covers and the steps it takes, for people new to the setup |
| `severity`, `group`, `site`, `description` | | as for checks |

## sites

Leave this out unless your things live in more than one place. With it, every entry names its site, and the dashboard shows each:

```yaml
sites: {home: Home, cabin: The cabin}
```

## Also

| Field | |
| --- | --- |
| `groups` | one line per group, in display order: `{media: Jellyfin and the photo library}` |
| `links` | footer links: `[{label: Grafana, url: "http://...", group: Dashboards}]` |
| `never_probe` | IP addresses no check may touch (someone else's machines on a shared network) |
| `github`, `repos` | for repos found by Connect GitHub: `repos: [{name: shop, check: web}]` links a repo to the check that shows it is up |
