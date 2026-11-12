# Installing Argus

## What you need

- **For Argus:** a machine that is always on, with Docker, on amd64 or arm64. A NAS, a mini PC, a Raspberry Pi 4 or 5, a small VM or LXC container all work.
- **For each host you add:** Linux on amd64 or arm64, with root, and systemd or OpenRC (Alpine: `apk add curl` first).
- **Optional:** a Telegram account for alerts, and a GitHub account for app updates and deploys.

| | Recommended | Enough to run |
| --- | --- | --- |
| CPU | 1 core | a share of one (it idles near 0%) |
| Memory | 512 MB | 256 MB |
| Disk | 1 GB | about 300 MB for the image, plus its data |

Measured with 18 hosts, 32 checks and about 40 GitHub repos: 150 MB of memory, 0.1% of a core, and 4.4 MB of data after a day.

## Start Argus

Use the `compose.yml` from the [README](../README.md#quick-start) (also in [examples/compose.yml](../examples/compose.yml)), then:

```bash
docker compose up -d
docker compose logs argus | grep "Setup code"
```

Open `https://<the machine>:8443`, enter that setup code and choose a password of at least 10 characters. The code proves that the person choosing the password can read Argus's log, so nobody else on your network can claim a fresh install.

### HTTPS

Argus serves HTTPS from the first start, with a certificate it makes itself, so your browser warns once. The log shows the certificate's fingerprint if you want to compare. Companions do not need a trusted certificate: the command that adds a host pins Argus's key.

- **Your own certificate:** mount it and set `ARGUS_TLS_CERT=/certs/fullchain.pem` and `ARGUS_TLS_KEY=/certs/privkey.pem`.
- **Behind a reverse proxy that does HTTPS:** set `ARGUS_TLS=off` and forward to port 8080.

### Settings from the environment

| Variable | Default | What it does |
| --- | --- | --- |
| `ARGUS_TLS` | `on` | `off` serves plain HTTP, behind a proxy |
| `ARGUS_TLS_CERT`, `ARGUS_TLS_KEY` | | your certificate instead of Argus's own |
| `ARGUS_DB` | `/data/argus.db` | the database; everything Argus keeps is in its folder |
| `ARGUS_CONFIG` | `/config/argus.yml` | a mounted `argus.yml`, if you prefer a file ([config.md](config.md)) |
| `ARGUS_DEPLOY_DIR` | | deploy from a folder instead of a GitHub repo |
| `ARGUS_PORT` | `8080` | the port inside the container |

Everything else is set in the UI. Next: follow the checklist on the home page, or read the [guide](guide.md).

## Upgrade

Argus has version numbers (`1.4.2`). A patch release fixes something, a minor release adds something, and a major release needs something from you, said in its release notes. The image tag `:1` follows every 1.x release.

- **Update Argus** (Settings, Account) pulls the new image and restarts Argus, then rolls back if the new one is not healthy within three minutes. It needs Argus's own machine added as a host, because that host's companion does the work. The header shows **Update available** when there is something new.
- **Without a companion there,** run `docker compose pull && docker compose up -d` beside Argus's `compose.yml`.
- **A new major version** is never pulled for you. Change the tag in `compose.yml` (`:2`) after reading its notes.

Companions update themselves within a minute or two of a new Argus, checking each download against a signature from Argus. Hosts that do not (monitoring-only hosts, or `"manual_updates": true` in the host's policy) show **companion out of date**, with a **Reinstall companion** button.

## Remove Argus

**Settings, Leave Argus** walks through it:

1. Export a backup, in case you come back.
2. Uninstall every companion at once. Each hands its stacks back as plain Compose folders in `/opt/stacks`, with their containers still running.
3. A checklist of what lives outside Argus, with a link for each: the GitHub App, the deploy repo's token and Dependabot file, the Telegram bots. Argus deletes nothing outside itself.

Then, beside Argus's `compose.yml`:

```bash
sudo docker compose down -v
```

`-v` removes Argus's data volume, which holds its settings and secrets.

## Forgot the password

```bash
docker compose exec argus python -m argus reset-login
```

Then open Argus and use the new setup code from its log.
