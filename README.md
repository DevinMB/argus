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

## Develop

```bash
uv venv .venv && uv pip install -p .venv -r requirements-dev.txt
PYTHONPATH=src .venv/bin/pytest -q && .venv/bin/ruff check .
cd ui && npm ci && npm test
```

CI runs lint, tests, an image build (which runs the UI tests), Trivy (fixable HIGH/CRITICAL fail) and a container smoke test.
