# How Argus is built

This page is for people changing Argus. To use it, read the [guide](guide.md). For what it holds and how that is protected, read [security.md](security.md).

## Goals

1. **Simple.** Every choice is in the UI, with a sensible default. A first deploy takes minutes.
2. **Few moving parts.** One server container and one companion binary. Nothing else to run.
3. **Nothing listens on a host.** Companions only make outbound calls.
4. **Git and the UI together.** People who keep config in git keep doing that; the UI edits the same file.
5. **No trust in the LAN.** Every host has its own token, and every reply to it is signed.

Out of scope: Kubernetes, Nomad and Swarm (Argus deploys Docker Compose); metrics and graphs (Argus records states and events; Prometheus and Grafana are the answer for graphs); more than one user account.

## The pieces

```
                 GitHub (app repos, deploy repo, GHCR)
                          ▲  polled every minute
                          │  (one GitHub App, connected once)
                ┌─────────┴─────────┐
   browser ───► │      Argus        │ ──► Telegram (or /metrics for Prometheus)
                │  one container:   │
                │  API, UI, SQLite  │
                └─────────▲─────────┘
                          │  HTTPS, outbound from each host only:
                          │  "here is my state" / "what should run on me?"
             ┌────────────┼────────────┐
        companion     companion     companion      one per host
```

**Argus** is one container: Python, FastAPI, SQLite and a React UI. It runs the checks, keeps settings and encrypted secrets, sends alerts, decides what each host should run and when it may patch, and polls GitHub.

**The companion** is one static Go binary for amd64 and arm64. Once a minute it reports what outside checks cannot see (containers, failed units, disks, pending updates, a needed reboot), runs checks from inside the host, and asks Argus what to do: deploy a stack, patch, update itself. It acts only on signed replies and only within the host's policy file. A host enrolled as monitoring only never deploys, patches or shares files.

**Enrollment.** Add a host makes a one-time code. The install command carries it with Argus's URL and TLS key pin. The companion pins that key, trades the code for its own token (Argus keeps only a hash), and pins Argus's ed25519 signing key. From then on a reply that does not verify is refused, so a proxy or a wrong DNS entry cannot make a host run anything.

**Config** is one YAML file, kept in Argus, in a GitHub repo (a UI edit becomes a commit or a pull request), or mounted read-only. Secrets never go to git.

## Decisions

Numbered, because code comments refer to them. Superseded ones stay, marked, so the numbers do not move.

1. **License: Apache-2.0.**
2. **HTTPS by default,** with a self-signed certificate that companions pin. Your own certificate or a proxy still works.
3. **Stack secrets are kept in Argus,** encrypted, and sent only to the host that runs the stack, inside its signed reply. The companion writes them to a root-only `.env`.
4. _Superseded by 7._ Host patching after launch.
5. **Viewing was open by default on the LAN.** Since 1.3, a new install requires sign-in to view, because the pages list each host's unpatched CVEs. Older installs keep their setting.
6. **A guided first run.** A checklist replaces the empty home page, one step at a time, and collapses to one line once started. No page is a dead end, and nothing needs a config file.
7. **Host patching ships with 1.0:** a weekly window per host, one host at a time, containers checked healthy afterwards.
8. _Superseded by 12._ Patching levels by safety net. Two rules from it stay: Argus watches only the backups linked to a host, so a host left out on purpose never alerts about backups; and nothing about a removed host passes to a later one with the same name.
9. **Restore tests are optional and encouraged.** A restore test reports as a job; the host's card shows when it last passed and nudges after 35 days.
10. **Argus's own data is backed up too:** an encrypted export of settings, secrets, hosts and certificate, and a reminder on the home page until one exists.
11. **Secrets are built in.** An external store (HashiCorp Vault, OpenBao, CyberArk) is a later, advanced option: the companion would resolve a reference on its host, so Argus never holds the value. Not chosen: GitHub Actions secrets (only a workflow can read them) and password managers made for people.
12. **No backup, no patching.** Argus patches a host only when it can be brought back: a backup that reports to Argus and is newer than the owner's limit (8 days by default), or a ZFS root it snapshots first. A stale backup skips the window and alerts. Why: if an update breaks a machine, the owner must be able to get it back, and only the owner knows how recent is recent enough.
    - **Amended:** the owner may say a host **needs no backup** because it can be rebuilt. Argus then patches it and says "Safety net: none, by your choice". An unpatched host is the larger risk.
13. **Argus reads Proxmox backups itself.** Most homelabs back up guests with vzdump, and without that decision 12 would patch nothing. The companion on a Proxmox server reads each backup storage's `dump/` folder in the background, so a hung NFS mount cannot stop reports, and reports each guest's newest backup. A guest's card picks the backup by the guest's name.
14. **Add Argus to a guest from its Proxmox server.** The server's companion runs the install inside the guest (`pct exec`, or the QEMU guest agent), building the command from checked fields only. Off on monitoring-only servers and with `"no_guest_installs": true`.
15. **One plan for all guests, a stricter rule for Proxmox servers.** Hosts, Patching turns one plan on for every guest with a recent backup. A server is patched only when every guest on it is backed up, one server at a time, never while another's newest run failed. Windows last six hours because hosts take turns, and a host whose turn never comes alerts as skipped.
16. **Argus updates itself through the companion on its own machine.** Argus never gets the Docker socket. Update Argus asks that companion to pull and recreate the Compose project, wait three minutes for health, and roll back if it fails.
17. **Deploy from git is a guided setup from an empty repo:** create the repo, give a write token for it, move running stacks in, turn on Dependabot. Argus's own stack stays out, since deploying itself would restart it mid-deploy.
18. **Letting go of Argus is supported, and so is coming back.**
    - **One host:** Uninstall (the admin password twice) has the companion hand every stack back as a plain Compose folder in `/opt/stacks/<name>`, leave its containers running, and remove itself.
    - **All of Argus:** Leave Argus exports a backup, uninstalls every companion, and lists what lives outside Argus with a link for each.
    - **Coming back:** a restored Argus plus Add a host takes the handed-back stacks over in place.
    - Why the password twice: uninstalling software on another machine is the strongest thing the UI can do.
19. **Hosts watch Argus, each with its own bot.** A stopped Argus cannot say it stopped, and companions hold no copy of Argus's bot token. `argus-companion watch` sets up a second bot on that host, and the token never leaves the host. After 5 minutes without a signed reply it sends "Argus is not answering", then a reminder every 6 hours. Update Argus carries a quiet window so it is not an outage. Not chosen: a second Argus (a whole install for one job).
20. **The app pipeline ships with Argus, one pull request per repo.** Add the pipeline reads a repo's Dockerfiles and opens a self-contained `ci.yml` (build, tests in the image, health check, Trivy, push to GHCR, the Dependabot gate) and, if missing, `dependabot.yml`. Argus never merges it, and never replaces a `ci.yml` it did not write. Build caches roll weekly, so `apt-get upgrade` layers do not go stale. Not chosen: a reusable workflow in Argus's repo (every user's CI would run code from Argus's `main`).
21. **Version numbers, from 1.0.0.** The release workflow tags `vX.Y.Z`; the tag's build pushes `:X.Y.Z`, `:X.Y` and `:X` and publishes release notes from merged pull requests. Patch: a fix or dependency update (the Dependabot gate starts one after each update it merges). Minor: something new. Major: something the owner must act on, never pulled for them. Update Argus reads releases.
22. **Urgent security fixes do not wait for the weekly window.**
    - **Urgent** means a fix for a CVE on CISA's Known Exploited Vulnerabilities list, or with a CVSS score of 9.0 or more, or called critical by the distro. High scores wait for the weekly run on purpose: a kernel collects them most weeks.
    - **The scores** come from CISA's list and cve.org, looked up by Argus. Both are public, and hosts need no internet.
    - **When:** at the host's usual patch hour, on any day, installing only those packages, under every rule of a weekly run. A Proxmox server gets an alert instead, since it is only updated as a whole.
    - **The owner sets the lines:** whether the exploited rule applies, the score that installs at once, and the score that alerts about a fix still waiting.
    - **For apps:** Argus alerts when a critical or exploited Dependabot alert's fix is a major update or fails its checks.
    - Not chosen: every security update nightly (more restarts, and "urgent" stops meaning anything); NVD's API (rate limits and backlogs); companions looking scores up.
23. **A container on an older image says so.** Argus asks well-known public registries which digest a tag points at now, and marks a running container whose image differs. It does not alert, pull or restart. Only known registries are asked, so a companion cannot aim Argus at an address of its choosing.
