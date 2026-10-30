# argus-next

Argus v2: monitoring and automated deploys for homelabs, with optional companions on each host. Developed here, privately, while Argus v1 (DevinMB/argus) runs the homelab; at launch this repo becomes DevinMB/argus and goes public.

Start with [docs/design.md](docs/design.md).

## Try it

```bash
docker build -t argus .
docker run -d --name argus -p 8080:8080 -v argus-data:/data argus
docker logs argus | grep "Setup code"
```

Open https://localhost:8080 (the certificate is Argus's own, so the browser warns once), go to **Alerts**, enter the setup code and a password, then add a Telegram bot and press **Send test**. Checks go in `argus.yml`, mounted at `/config`.

- `ARGUS_TLS=off` serves plain HTTP (behind a proxy); `ARGUS_TLS_CERT` and `ARGUS_TLS_KEY` use your own certificate.
- Forgot the password: `docker exec argus python -m argus reset-login`.

## Getting started

A new install shows a short checklist on its home page: make sure alerts reach you, add a host, watch what matters, connect GitHub, deploy from git. One step is open at a time, with a line on why it matters and one button. Once a host reports, Argus suggests a check for each port its containers publish ("jellyfin :8096 on nas"); **Watch** fills in the form. Afterwards a coverage line ("12 of 15 published services watched · 4 of 4 hosts reporting") and each host's "not watched yet" list show what to do next. The checklist can be hidden.

## Patching

Each host's card has **Choose a window**: a weekday and time on the host's own clock. In the window, the host's companion asks Argus, and Argus decides. It never patches on a Sunday (backups run then, unless you allow it for a host), patches one host at a time, and never patches a monitoring-only host. Updates run in a transient systemd unit, outside the companion's sandbox. Afterwards every container that ran before must run again (and be healthy, if it has a health check), then the host reboots if an update needs it and its safety net allows it. A failed run, a run with no result for two hours, or a window skipped for a stale backup pages you. **Patch now** runs once, outside the window, by the same rules.

How far Argus goes depends on the host's safety net. No backup location is required:

| Safety net | Argus installs |
| --- | --- |
| None, or a backup never restore-tested | security updates only, no reboots |
| A fresh backup you accepted untested, a fresh backup with a restore test in the last 35 days, or a ZFS root (snapshotted before each run) | all updates, and reboots when needed |

A backup or restore test is any job or timer that reports to Argus (`POST /job/<name>` or `/heartbeat/<name>`) when it finishes. Name it on the host's card; no fresh backup (8 days) means no patch that week, and Argus says why. Restore tests are optional, and encouraged: they are how you know a backup works.

## Config

Everything Argus watches is one YAML file, `argus.yml`. Change single checks on the Checks page (**Add a check**, or **Change** in a check's panel), or the whole file on the Config page. Argus checks every change before saving; a mistake is shown and nothing changes. Comments and layout in the file are kept: an edit changes only its own lines.

Where the file lives (Config page):

- **In Argus** (a new install's default), with Export always there.
- **In a GitHub repo.** A change made in git shows within a minute; an edit made here becomes a commit, or a pull request to review. If the file changed in git since you opened it, the edit is refused and the newer version is shown. Saving needs the write token (GitHub page).
- **A mounted file** (`ARGUS_CONFIG`), read when it changes; the UI only shows it.

## Connect GitHub

On **Updates**, press **Connect GitHub**. Argus makes its own GitHub App on your account (or an organization) with GitHub's app-manifest flow: confirm on GitHub, then choose the repos it may see. Updates then shows each repo's pipeline: Dependabot's PRs, CI, the merge, the build and its Trivy scan. Majors are spotted from Dependabot's titles and wait for you.

- Read-only permissions. The app's private key is kept encrypted in Argus and never shown.
- Argus polls GitHub every minute with conditional requests (a 304 costs no rate limit), so nothing needs to reach Argus from the internet.
- Cannot make apps? Paste a fine-grained read-only token on the GitHub page instead.
- Archived repos are skipped; hide others on the GitHub page. In `argus.yml`, `repos:` can link a repo to its check (`{name: shop, check: web}`).

## Add a host

On **Hosts**, press **Add a host** and run the command it shows on the host. It installs the companion (a single static binary, amd64 or arm64) as a systemd service. Within a minute the host shows its containers, disks, failed systemd units, pending updates and whether it needs a reboot.

- The companion only calls Argus; nothing on the host listens. Its first call pins Argus's TLS key, and every reply from Argus is signed with a key it pins at enrollment.
- The code in the command works once, for 15 minutes. Removing a host revokes its token at once.
- A check with `companion: <host>` in `argus.yml` runs from inside that host, for services Argus cannot reach.
- A host that misses three reports, or a disk past 95%, pages.
- `argus-companion report` prints what it would send, without sending it.

## Deploys

One repo says what each host runs: `hosts/<host>` lists stack folders (one per line), and each folder holds a `compose.yml` and any files next to it. Pick it on the GitHub page. Each host's companion deploys its stacks when they change: `docker compose pull`, then `up --wait` until healthy, and back to the previous files if it is not.

- **The host's policy decides.** `/etc/argus-companion/policy.json` on each host lists what it will never run: by default privileged containers, the host's network or PID namespace, the Docker socket, devices, risky capabilities, and binds from outside `/srv`, `/opt`, `/mnt`, `/media` or the stack's own folder. Argus cannot change it; a stack that breaks it is refused before anything is written, and pages.
- **Secrets** for a stack are set on the Hosts page, kept encrypted in Argus, and written to the stack's `.env` (root only) on the host that runs it, nowhere else.
- **Private images:** the companion pulls with a credential Argus sends with that deploy only; it is never stored on the host.
- Hosts enrolled **monitoring only** never deploy.
- Stacks live in `/var/lib/argus-companion/stacks/<name>`; data a stack keeps in its folder (`./data`) is never touched by a deploy.
- Without GitHub, `ARGUS_DEPLOY_DIR` points Argus at a folder with the same layout.
- **Tag bumps:** when a repo's main build is green, Argus rewrites `ghcr.io/<owner>/<repo>[-part]:sha-<commit>` in the deploy repo in one fast-forward commit, and the hosts deploy it. This needs a fine-grained token for the deploy repo alone (Contents: read and write); the GitHub App stays read-only. Updates then follows each change to "deployed" from the companions' own reports.

## Develop

```bash
uv venv .venv && uv pip install -p .venv -r requirements-dev.txt
PYTHONPATH=src .venv/bin/pytest -q && .venv/bin/ruff check .
cd ui && npm ci && npm test
cd companion && go vet ./... && go test ./...
```

CI runs lint, tests, an image build (which runs the UI and companion tests), Trivy (fixable HIGH/CRITICAL fail), a container smoke test, and the Add a host command run for real on the runner.
