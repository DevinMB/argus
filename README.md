# Argus

**Keep a homelab watched, patched and deployed, from one container.**

Argus checks your services and alerts you when one stops answering. A small companion on each host shows its containers, disks and pending updates, patches it in a weekly window once it has a recent backup, and deploys your Compose stacks from git when you merge. No Prometheus, Grafana or Alertmanager needed, and no config files unless you want them.

![The Argus home page: a ring of every check with one down, a line saying that nothing that alerts you is down, and the services Argus suggests watching](docs/screenshots/home.png)

## What it does

- **Watches** anything with a URL, a port or an address, every minute; three failures in a row and it alerts you on Telegram (or hands off to your Prometheus). Pause one you already know is down.
- **Knows your hosts.** One command adds a host; Argus then shows its containers, disks, failed services and pending updates, with the severity score of each CVE an update fixes; it says when a container runs an older image than its tag now points at, and suggests a check for every port its containers publish.
- **Patches** each host on the weekly schedule you choose, one host at a time, once it has a backup Argus can see. You decide how recent that backup must be, how far updates go, and whether it may reboot. A fix for a flaw that is being exploited or is rated critical does not wait for the window: it goes in that night. Already patching a host another way? Tell Argus and it leaves that host alone.
- **Deploys from git.** Keep each host's Compose stacks in a repo; a merge deploys them, a stack that does not come up healthy is rolled back, and stacks already running can be adopted in place with one button.
- **Follows your apps' updates** on GitHub: Dependabot's PRs, CI, merges and builds, to "running on its host".
- **Guides you in.** A new install opens on a short checklist that takes you from nothing to your first host watched, patched and deploying.

## A look around

**Every host on one page**, with what needs a look on each and when it is patched. Each tag leads to its cause.

![Hosts: three hosts, with tags for a container that is down, a full disk and an urgent security fix, and each host's patch day](docs/screenshots/hosts.png)

**One host:** its disks and pending updates. Every CVE an update fixes carries its severity score, and a fix for a flaw that is being exploited is marked.

![One host: its disks and pending security updates, each CVE with its severity score, and one fix marked as being exploited](docs/screenshots/host.png)

**Urgent fixes do not wait for the weekly window.** The host's Patching tab says what goes in tonight and why.

![A host's Patching tab: all updates on Tuesdays at 03:00, and an urgent fix for sudo that Argus installs tonight](docs/screenshots/host-patching.png)

**You set the rules:** a day and an hour per host, the backup it needs first, and how serious a flaw must be to go in the same night.

![Hosts, Patching: each host's schedule and safety net, and the rule for urgent security fixes](docs/screenshots/patching.png)

**Containers**, with a note when a registry serves a newer image under the tag one of them runs.

![A host's Containers tab: five containers, one marked "newer image out"](docs/screenshots/containers.png)

**On a phone** it is the same page, not a cut-down one.

<img src="docs/screenshots/phone.png" width="320" alt="The Hosts page on a phone">

## Quick start

```yaml
# compose.yml
services:
  argus:
    image: ghcr.io/devinmb/argus:1
    ports: ["8443:8080"]
    volumes: [argus-data:/data]
    cap_add: [NET_RAW]   # for ping checks
    restart: unless-stopped
volumes:
  argus-data:
```

```bash
docker compose up -d
docker compose logs argus | grep "Setup code"
```

### What to give it

| | Recommended | Enough to run |
| --- | --- | --- |
| CPU | 1 core | a share of one (it idles near 0%) |
| Memory | 512 MB | 256 MB |
| Disk | 1 GB | about 300 MB for the image, plus its data |
| Architecture | amd64 (x86-64) or arm64 (a Raspberry Pi 4 or 5) | |

Measured on a homelab with 18 hosts, 32 checks and about 40 GitHub repos: 150 MB of memory, 0.1% of a core, and 4.4 MB of data after a day. Data grows slowly with check history and patch logs. A small VM or LXC container with 1 core, 1 GB of memory and 4 GB of disk runs Argus with room for Docker itself.

Open **https://your-host:8443**. Argus makes its own certificate, so the browser warns once. Enter the setup code and choose a password, then follow the checklist. Want to look around first? [`scripts/demo.py`](scripts/demo.py) fills a fresh install with a made-up homelab.

## Learn more

- [Install](docs/install.md): requirements, the first sign-in, HTTPS and proxies, upgrading, removing a companion.
- [Guide](docs/guide.md): checks, hosts, patching and backups, deploys and adopting stacks, GitHub, backing up Argus.
- [Config](docs/config.md): every field of `argus.yml`, for people who prefer files.
- [Security](docs/security.md): what Argus holds, how it is protected, and what a companion can and cannot do.
- [Design](docs/design.md) and [developing Argus](docs/develop.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
