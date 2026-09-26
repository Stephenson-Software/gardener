# The hub (docs/HUB.md): `gardener hub serve` in a container, for a host
# that runs gardener only as the shared run-history store. Dispatching
# (`tend`/`overnight`) needs git, gh and claude and is not what this image
# is for, so none of them are installed.
FROM python:3.12-slim

# gardener is stdlib-only, so the install pulls nothing beyond the package.
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY gardener ./gardener
RUN pip install --no-cache-dir . && rm -rf /src

# Non-root, and /data created owned by that user so a fresh named volume
# mounted there inherits the ownership and the store is writable.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin gardener \
    && mkdir /data && chown gardener:gardener /data
USER gardener
WORKDIR /data
VOLUME /data

# GARDENER_STATE_DIR points the few paths gardener still resolves there
# (hub.env, notify.env) at the volume too, never at the image.
ENV GARDENER_STATE_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8765
# /healthz is the one unauthenticated route and returns no data. python,
# not curl or wget: the slim image has neither.
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=5)"]

# Refuses to start without an operator credential (see docs/HUB.md), so a
# misconfigured container exits instead of serving the history openly.
CMD ["gardener", "hub", "serve", "--host", "0.0.0.0", "--port", "8765", "--data-dir", "/data/hub"]
