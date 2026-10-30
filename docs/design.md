# Argus v2 design

Status: draft for Devin's approval, 2026-10-30. Nothing here is built yet.

## What v2 is

Argus v1 runs one homelab: it checks every service, shows each app's path from update PR to production, and records the patch runs. Everything around it (deploys, patching, paging) is this homelab's own tooling: Ansible, stack-sync, stack-bump, Prometheus and Alertmanager.

Argus v2 is the same idea as a product anyone can run: **install one container, add a companion to each host, connect GitHub, and merges to main deploy themselves while Argus watches and pages.** No Prometheus, Alertmanager or Grafana needed, and no config files to edit unless you want them.

The main value is the automated deploys of infrastructure and containers. Monitoring is how Argus proves a deploy worked.

### Hard requirements (Devin)

1. **Simple and intuitive.** Every choice a user has to make is in the UI, with sensible defaults. A first deploy takes minutes, not an afternoon.
2. **Fewest moving parts.** One server container, one companion binary. Nothing else to run.
3. **Alerts chosen in the UI.** One page decides who sends alerts: Argus (Telegram first) or Prometheus and Alertmanager. Switching is never a gap.
4. **Git and the UI together.** People who keep config in git (like this homelab) keep doing that; the UI edits the same config.
5. **Tokens, not trust in the LAN,** without making setup harder.
6. **Dogfooding.** This homelab moves onto v2 and the same companion everyone else gets.

### Not in v2

- Snapshots and rollback of whole VMs. These need hypervisor access (Proxmox today), and that is a different integration. This homelab keeps its Ansible patch runs, reporting to v2 as jobs, as it does to v1.
- Kubernetes, Nomad, Swarm. v2 deploys Docker Compose stacks.
- Metrics and graphs. Argus records states and events, not time series. Prometheus and Grafana remain the answer for graphs.
- Multiple users and roles. One admin account.
- Anything that listens on a host. Companions only make outbound connections.

## The pieces

```
                 GitHub (app repos, deploy repo, GHCR)
                          ▲  polls every minute; writes tag bumps
                          │  (one GitHub App, connected once)
                ┌─────────┴─────────┐
   browser ───► │      Argus        │ ──► Telegram (or /metrics for Prometheus)
   (login for   │  one container:   │
    settings)   │  API, UI, SQLite  │ ──► external dead-man's switch (optional)
                └─────────▲─────────┘
                          │  HTTPS, outbound from each host only:
                          │  "here is my state" / "what should run on me?"
             ┌────────────┼────────────┐
        companion     companion     companion      one per host, or one
        (host A)      (host B)      (Pi)           container next to Docker
```

### Argus (the server)

One container, as in v1: Python, FastAPI, SQLite, the React UI. v1's code is the starting point: checks, heartbeats, jobs, the GitHub poller and the Updates view all carry over.

New in v2:

- **Any number of sites.** v1 has exactly two (A and B). A site is just a name and an order.
- **A settings store.** Settings live in SQLite. Secrets (bot tokens, the GitHub App key, stack secrets) are encrypted with a key Argus generates on first start and keeps in its data volume, outside the database. They are shown masked and can only be replaced, never read back.
- **A login,** for settings only by default (see below).
- **A notifier** (Telegram first).
- **Companion management:** enrollment, tokens, desired state.
- **The deploy engine:** it decides what each host should run and bumps image tags (v1's stack-bump, moved inside Argus).

### The companion

One small Go program, a single static binary for amd64 and arm64. It is also published as a container image, for hosts where people prefer that. It is idle almost all the time: once a minute it reports and asks for its desired state.

It does three things:

1. **Reports state** that outside checks cannot see: each container's state and health, failed systemd units, a disk running out, pending updates, and whether a reboot is needed.
2. **Runs local checks:** HTTP, TCP and ping from inside the host, for services that Argus cannot reach.
3. **Deploys:** it fetches its desired state from Argus, and for each stack that changed runs `docker compose pull` and `docker compose up -d`, waits for the containers to be healthy, and reports the result.

Read-only mode (no deploys, no Docker writes) is a single switch at install, for people who only want monitoring.

## Setting up (what the user does)

1. **Start Argus:** one `docker run` or a compose snippet from the README. The first page asks for an admin password. A setup code printed in the container log is required to set it, so nobody else on the network can claim a fresh install.
2. **Alerts:** the Alerts page asks who sends alerts. For Telegram, paste a bot token and a chat ID and press **Send test**.
3. **Add a host:** click "Add a host", copy one command, and run it on the host. The host appears within a minute.
4. **Connect GitHub:** click "Connect GitHub", approve the app on the repos you choose, and pick the deploy repo.
5. Optional: **add checks** for anything with a URL or port.

## Companions in detail

### Enrollment

- "Add a host" creates an **enrollment code**: single use, valid for 15 minutes. The install command carries it, together with Argus's URL and Argus's certificate fingerprint.
- The companion makes its first call over HTTPS and **pins that fingerprint**. Argus can then use a self-signed certificate on a LAN with no man-in-the-middle risk, and with no certificate setup.
- The companion trades the code for its own **companion token** (256 random bits). Argus stores only a hash of it.
- Removing a host in the UI revokes its token at once.

### Desired state, signed

- Argus keeps an **ed25519 signing key**. The companion pins its public key at enrollment.
- Every desired-state response is signed, and a companion refuses anything that does not verify. A wrong TLS setup or a proxy in between cannot make a host run something else.
- **Companion policy, on the host:** a local file says what this host will never run, whatever it is told. By default that is privileged containers, host network, mounts outside an allowed list, and the Docker socket. Changing it needs someone on the host. This limits what a compromised Argus or a malicious commit to the deploy repo could do.

### Secrets for stacks

v1 keeps stack secrets age-encrypted in git and Ansible writes them to each host. In v2:

- Stack secrets are entered in the UI, or imported, and kept encrypted in Argus.
- They are sent only to the host that runs that stack, inside its signed desired state.
- The companion writes them to a root-only `.env` next to the compose file, as Ansible does today.

### Private images

- Argus gives each companion a **short-lived, read-only pull credential** with its desired state. The companion uses it for that pull only and never stores it.
- The planned source is a GitHub App installation token with packages read. The first task of M4 is to prove GHCR accepts it.
- If it does not, the fallback is a long-lived read-only pull token kept in Argus and passed the same way. Hosts still hold no credentials of their own.

## GitHub

- **One connection, in Argus.** "Connect GitHub" uses GitHub's app-manifest flow: Argus creates its own GitHub App on the user's account in a couple of clicks, then the user installs it on the repos they choose. A fine-grained token is the fallback for people who cannot use an app.
- Permissions, kept to the minimum:
  - read: contents, pull requests, actions, Dependabot alerts, packages;
  - write: contents, on the deploy repo only.
- **Polling, not webhooks,** by default: every minute, so it works behind NAT with nothing exposed. A webhook URL is an optional speed-up for people whose Argus is reachable.
- The **pipeline** stays as in v1:
  1. Dependabot opens PRs; CI tests and scans them.
  2. Patch and minor updates merge on green; majors wait for the owner.
  3. main builds an image with a SHA tag.
  4. Argus bumps the tag in the deploy repo.
  5. The companion deploys it.
  6. Argus shows it healthy.

  v2 does not build images: that stays in the app's CI. v2 ships the reusable workflows (today's DevinMB/workflows) as a template.

## Config: git and the UI together

The config source is one of two, chosen at setup and changeable later:

- **Argus itself** (the default). UI edits are saved in the database. Export and import as YAML are always available.
- **A git repo,** as this homelab does today.
  - Argus reads the config from the repo every minute.
  - A UI edit becomes a commit to the repo, or a PR if the user prefers reviews.
  - A change made in git shows in the UI within a minute.
  - When an edit and a commit collide, the UI shows the newer git version and asks the user to redo the edit.

Secrets never go to git, in either mode.

## Alerts

The Alerts page asks one question: **who sends your alerts?**

- **Argus** (the default):
  - Telegram first; the channel is a small interface, so ntfy, email and webhooks fit later without a redesign.
  - A short list of what pages, with defaults: a critical check goes down or recovers; a job fails or is overdue; a companion or heartbeat goes quiet; a deploy fails.
  - Repeats while something stays down, at an interval the user picks.
- **Prometheus and Alertmanager:**
  - `/metrics` as today, with downloadable alert rules and an Alertmanager snippet.
  - **Handoff:** Argus keeps sending until the user confirms that Alertmanager's test alert arrived; only then does it turn its own off. The same in reverse.

### Who watches Argus

A stopped Argus cannot page about itself. The Alerts page offers two optional watchers:

- **An external dead-man's switch:** Argus pings a URL the user pastes (for example healthchecks.io's free tier) every minute, and that service alerts when the pings stop.
- **A peer Argus:** two Argus instances watch each other, as the two sites do here.

The page says plainly when neither is set up.

## Login

- One admin account. The password is set on first start, using the setup code from the log.
- Viewing is open by default, as in v1. One switch requires the login for everything.
- Passwords are hashed with argon2id. Sessions are HTTP-only, SameSite=Strict cookies, every write is CSRF-protected, and login attempts are rate-limited.
- HTTPS is on by default with a self-signed certificate; the user can bring their own or put Argus behind a proxy.

## Migration from v1 (this homelab)

| v1 piece | In v2 |
| --- | --- |
| Argus v1 | replaced by v2 at launch; repo renamed `argus-v1` and archived |
| `argus.yml` in homelab-ops | stays, with git as the config source |
| stack-sync on each Docker host | the companion's deploys |
| stack-bump on ops-runtime | the deploy engine inside Argus |
| mars-sync | stays as is (it runs MARS's own deploy script), reporting through its companion |
| Per-host deploy keys | removed; companions fetch from Argus |
| Prometheus, Alertmanager, Grafana | stay; Alerts page set to Prometheus |
| Ansible patch runs, ops-apply | stay, reporting as jobs |

The move happens host by host, with v1's stack-sync left in place until the companion on that host has deployed successfully.

## Milestones

Each milestone ships something usable and goes through PRs with tests, like v1.

| | Milestone | Done when |
| --- | --- | --- |
| M0 | `argus-next` repo, CI, v1 code imported, two sites generalized to many | v1's tests pass with any number of sites |
| M1 | **Standalone Argus:** login, settings store, encrypted secrets, Alerts page with Telegram, Send test, handoff to Prometheus, dead-man ping | a fresh install pages a phone with no Prometheus |
| M2 | **Companions, monitoring:** enrollment, tokens, state reports, local checks, read-only mode | a host added with one command shows its containers and disks |
| M3 | **Connect GitHub:** the App flow, the Updates view generalized | a fresh install shows its repos' pipeline |
| M4 | **Deploys:** desired state, signing, companion policy, stack secrets, pull credentials, tag bumps | a merge to main deploys to a companion with no other tooling |
| M5 | **Git-backed config:** reading the repo, UI edits as commits or PRs | this homelab's argus.yml is edited from the UI |
| M6 | **Launch:** README, install guide, example config, screenshots, license; repo swap; this homelab on v2 | someone else could install it from the README alone |
| after | **Security review and hardening** (see the roadmap) | findings fixed or documented |

M0 and M1 are the plan for Friday.

## Decisions for Devin

1. **License.**
   - MIT: simplest; anyone can do anything.
   - Apache-2.0: like MIT, plus a patent grant.
   - AGPL-3.0: anyone who offers a modified Argus as a service must share their changes.

   Suggestion: **Apache-2.0**.
2. **HTTPS by default with a self-signed certificate,** pinned by companions. The alternative is plain HTTP on the LAN as in v1, which is simpler but weaker. Suggestion: **HTTPS by default**.
3. **Stack secrets kept in Argus,** delivered only to their host, as described above. The alternative is to keep sops/age in git, but then every host needs a decryption key. Suggestion: **in Argus**, with import from age files for this homelab.
4. **Host patching through companions** (apt and dnf upgrades on a schedule, with reboot windows and health checks, but without snapshots) as a later milestone after M6. Suggestion: **yes, after launch**; v2 does deploys first.
5. **Viewing open by default** on the LAN, as in v1. Suggestion: **yes**, with the login switch one click away.
