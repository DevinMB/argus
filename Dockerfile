# The read-only UI: built and unit-tested here, shipped as static files only.
FROM node:26.12.0-alpine AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ui/.npmrc ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm test && npm run build

FROM python:3.14.7-slim-trixie

# iputils-ping for ping checks. The container needs CAP_NET_RAW for it (compose adds it).
# upgrade: Debian security fixes land between base image rebuilds, and Trivy gates on them.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && apt-get install -y --no-install-recommends iputils-ping \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# pip leaves after installing: it vendors its own certifi, msgpack and urllib3,
# which Trivy rightly flags, and the running service installs nothing.
RUN pip install --no-cache-dir -r requirements.txt \
 && pip check \
 && pip uninstall -y pip setuptools wheel \
 && rm -rf /root/.cache /usr/local/lib/python3*/ensurepip \
 && python -c "import fastapi, uvicorn, httpx, yaml, prometheus_client"
COPY src/ src/
COPY --from=ui /ui/dist ui/dist/

ARG COMMIT=unknown
ENV ARGUS_COMMIT=$COMMIT ARGUS_VERSION=2.0.0-dev PYTHONPATH=/app/src PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]
CMD ["python", "-m", "argus"]
