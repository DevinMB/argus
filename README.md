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

## Add a host

On **Hosts**, press **Add a host** and run the command it shows on the host. It installs the companion (a single static binary, amd64 or arm64) as a systemd service. Within a minute the host shows its containers, disks, failed systemd units, pending updates and whether it needs a reboot.

- The companion only calls Argus; nothing on the host listens. Its first call pins Argus's TLS key, and every reply from Argus is signed with a key it pins at enrollment.
- The code in the command works once, for 15 minutes. Removing a host revokes its token at once.
- A check with `companion: <host>` in `argus.yml` runs from inside that host, for services Argus cannot reach.
- A host that misses three reports, or a disk past 95%, pages.
- `argus-companion report` prints what it would send, without sending it.

## Develop

```bash
uv venv .venv && uv pip install -p .venv -r requirements-dev.txt
PYTHONPATH=src .venv/bin/pytest -q && .venv/bin/ruff check .
cd ui && npm ci && npm test
cd companion && go vet ./... && go test ./...
```

CI runs lint, tests, an image build (which runs the UI and companion tests), Trivy (fixable HIGH/CRITICAL fail), a container smoke test, and the Add a host command run for real on the runner.
