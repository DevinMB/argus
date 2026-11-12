# Using Argus

What each part of Argus does. Installing and upgrading: [install.md](install.md). How Argus protects what it holds: [security.md](security.md). Every config field: [config.md](config.md).

## Getting started

A new install opens on a short checklist: add a host, make sure alerts reach you, know if Argus stops, watch what matters, set up patching, connect GitHub, deploy from git. One step is open at a time, with one button. Once you start, it shrinks to one line at the top of each page, and it can be hidden.

## Checks

A check watches anything with a URL, a port or an address, every minute. Three failures in a row and it is down.

- **Checks, Watch more** lists every port your hosts' containers publish that no check covers yet, and each Proxmox server's web UI. Choose **Off**, **Watch** (shows a problem in Argus) or **Alert** (also sends you a message) for each, then **Add**.
- **A website or service** adds anything else, such as a public URL or a printer. **Import** reads a pasted `argus.yml`.
- A check can run from inside a host, for services Argus cannot reach: pick the host when you add it.
- **Pause** a check you already know is down, with a reason. It resumes by itself once it answers again, or stays paused until you resume it. A paused check keeps its history but never alerts and is left out of every count.

Everything Argus watches is one YAML file, `argus.yml`. Most people never open it: the Checks page edits it and keeps its comments and layout. It can live in Argus (the default), in a GitHub repo (an edit becomes a commit or a pull request), or in a mounted file. The Config page shows the whole file.

## Alerts

**Alerts, Who sends your alerts** picks the sender:

- **Argus, on Telegram.** Paste a bot token from @BotFather, send your bot a message, and Argus finds the chat. **Send a test** proves it works.
- **Prometheus and Alertmanager**, if you already run them. Argus serves `/metrics` and the rules to copy. It keeps sending its own alerts until Alertmanager's test arrives, so the switch leaves no gap.

**What alerts you** sets the rules: a check set to Alert goes down or comes back, a host stops reporting or a disk passes 95%, a job fails or is late, a patch run fails, a security fix is waiting.

**Who watches Argus.** A stopped Argus cannot tell you it stopped. On any host, run `sudo /usr/local/bin/argus-companion watch` and give it a second Telegram bot (not the one Argus uses). That host then messages you if Argus stops answering for 5 minutes. The token stays on that host.

## Hosts

On **Hosts**, press **Add a host** and run the command it shows on the host. Within a minute the host shows its containers, disks, failed services, pending updates and whether it needs a reboot. One command can add several hosts; each takes its own name.

- A host that misses three reports, or a disk past 95%, alerts you.
- Each tag under **Needs a look** links to its cause.
- **Ignore** a container you do not care about (a one-off that exited). It moves to the bottom and no longer counts as trouble.
- **Newer image out** on the Containers tab means the registry now serves a newer image under the tag a running container uses (`postgres:16`, `:latest`). Pull it in the stack's folder, or let its next deploy do it. A tag naming one exact version never moves; for those, let Dependabot propose the next version.
- **Report refused** means the host is running but Argus cannot read what it sends. The host's page says what was wrong and keeps showing its last good report. Updating Argus usually clears it.
- **Rename** on a host's page fixes a bad hostname (`localhost`, two servers both called `pve`). Its patch plan and history come along.
- **On a Proxmox server**, the guest list has **Add Argus** for each guest (and **Add Argus to all**): the server installs the companion in the guest for you. VMs need the QEMU guest agent running.

**Uninstall** (under **Manage** on a host's page) asks for the admin password twice. The companion hands every stack Argus created back as a plain Compose folder in `/opt/stacks/<name>`, leaves the containers running, and removes itself. A host that is offline gets the command to run on it instead: `sudo /usr/local/bin/argus-companion uninstall`. **Remove** only forgets a host that is gone for good.

## Patching

Argus patches a host only when it can be brought back. Give each host a **safety net**:

- **A recent Proxmox backup.** On a Proxmox server, Argus reads each guest's newest vzdump backup. A guest's card picks its backup by name; nothing to wire.
- **A recent backup of another kind:** a backup job that reports to Argus when it finishes. Say how recent it must be: 1 day for a nightly backup, 8 for a weekly one.
- **A ZFS root**, which Argus snapshots just before each run.
- **Backed up outside Argus**, with a note saying where. Argus takes your word.
- **It needs no backup**, for a host you could simply rebuild.

Then choose a **window**: a weekday and a time on the host's clock. **Hosts, Patching** sets one plan for every guest at once, and gives each Proxmox server its own day.

In the window, one host at a time, Argus installs what you chose (all updates, or security only). Afterwards every container that ran before must run again, then the host reboots if an update needs it and you allowed reboots. A failed run, a stale backup or a window that ended before the host's turn alerts you. **Patch now** runs once, outside the window, by the same rules.

**Several hosts at once.** **Hosts, Patching** lists every host with updates waiting, with the ones that have security updates ticked. **Patch N hosts now** patches each by its own plan, one at a time. A host that cannot be patched says why. A Proxmox server is never ticked for you, because its update can restart every guest on it.

**Urgent security fixes** go in the same night instead of waiting for the window. Urgent means a fix for a flaw that is being used in real attacks (on CISA's Known Exploited Vulnerabilities list) or rated critical (CVSS 9.0 or more). Argus installs only those packages, at the host's usual hour, under the same rules, and sends you a message saying what went in.

- Every pending CVE shows its CVSS score (out of 10) on the host's page: **low** under 4, **medium** to 6.9, **high** to 8.9, **critical** from 9.
- Change what counts as urgent under **Hosts, Patching, Urgent security fixes**. Change when a waiting fix alerts you under **Alerts, What alerts you**.
- Turn it off for one host with **Install urgent security fixes the same night**.
- Alpine hosts have no urgent runs, because apk does not mark security fixes. A Proxmox server gets an alert instead, since it is only updated as a whole; **Patch now** takes the fix.

**Proxmox servers** follow a stricter rule: a server is patched only when every guest on it has a recent backup, one server at a time, always with all updates. A new major version (9 to 10) is never taken.

**Already patched by something else** (Ansible, unattended-upgrades)? Choose **Something else patches it**, and Argus leaves that host alone. Name the job that does it, and Argus alerts if that job fails.

**Restore tests** prove a backup works. Argus cannot open your backup, but it can watch a restore test that reports as a job. The host's card shows when it last passed. It is optional, and encouraged.

## GitHub

On **Updates**, press **Connect GitHub**. Argus makes its own read-only GitHub App on your account, and you choose the repos it may see. (Cannot make apps? Paste a read-only token instead.) Updates then shows each repo's path from a Dependabot pull request to running on its host.

- **No CI yet?** Open a repo and press **Add the pipeline**. Argus opens a pull request with a workflow that builds each image, tests it, scans it with Trivy, pushes it to GHCR, and merges Dependabot's patch and minor updates once they pass. You review and merge it. The first time, Argus links you to the app's permissions to allow it.
- **Majors wait for you.** If a critical security fix is a major update, or fails its checks, Argus alerts you with the app, the package and the pull request.

## Deploys

**Set up deploys** walks through four steps:

1. **A repo for your stacks.** Create an empty private repo; Argus writes the first commit.
2. **A write token** for that repo only.
3. **Move your stacks in.** Every Compose project running on your hosts is listed. **Move into git** commits each compose file, keeps its `.env` secrets in Argus, and leaves the containers running where they are.
4. **Turn on Dependabot,** so a newer image arrives as a pull request. Merging it deploys it.

From then on, each host deploys its stacks when they change in git: pull, start, wait until healthy, and go back to the previous version if it is not.

- **Your own apps:** when an app's main build passes, Argus updates its image tag in the deploy repo, and the host deploys it.
- **Secrets** are set on the Hosts page and written only to the `.env` on the host that runs the stack.
- **The host decides what it runs.** Each host's `/etc/argus-companion/policy.json` refuses privileged containers, the Docker socket, the host's network and folders outside a short list. A stack that breaks it is refused and alerts you. See [security.md](security.md).
- **Adopting one stack** from a host's **Deploys** tab works the same as Move in, with a preview first.
- Without GitHub, `ARGUS_DEPLOY_DIR` points Argus at a folder with the same layout.

## Back up Argus

**Settings, Account, Back up Argus** exports one encrypted file with everything a new install needs: settings, secrets, hosts and Argus's certificate. Keep it off the machine Argus runs on. **Restore from an export** on a fresh install carries on where the old one stopped. The home page reminds you until there is a backup.
