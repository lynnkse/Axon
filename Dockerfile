# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE} AS runtime

ARG AXON_IMAGE_VERSION=dev
ARG AXON_GIT_COMMIT=unknown
ARG BUILD_TIMESTAMP=unknown

LABEL org.opencontainers.image.title="Axon engine" \
      org.opencontainers.image.version="${AXON_IMAGE_VERSION}" \
      org.opencontainers.image.revision="${AXON_GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIMESTAMP}" \
      org.opencontainers.image.description="Offline-capable Axon engine process image"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/axon/home \
    SOCKET_DIR=/run/axon \
    RELAY_DIR=/var/lib/axon/relay \
    PROJECT_DIR=/workspace \
    PROFILE_PATH=/etc/axon-instance/profile.md \
    PATH=/opt/axon/container/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 axon \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/axon/home --no-create-home axon

WORKDIR /opt/axon
COPY container/requirements-engine.txt container/requirements-container.txt /tmp/axon-requirements/
RUN python -m pip install --no-cache-dir \
      -r /tmp/axon-requirements/requirements-engine.txt \
      -r /tmp/axon-requirements/requirements-container.txt

COPY --chown=root:root . /opt/axon
RUN chmod 0755 /opt/axon/container/entrypoint.sh \
      /opt/axon/container/healthcheck.py \
      /opt/axon/container/pty_supervisor.py \
      /opt/axon/container/bin/fake-claude \
      /opt/axon/container/bin/fake-codex \
    && mkdir -p /etc/axon-instance /workspace /var/lib/axon/home \
      /var/lib/axon/relay /var/log/axon /run/axon /tmp/axon-tmp \
    && chown -R 10001:10001 /workspace /var/lib/axon /var/log/axon /run/axon /tmp/axon-tmp \
    && chmod 0700 /workspace /var/lib/axon /var/log/axon /run/axon /tmp/axon-tmp

USER 10001:10001
ENTRYPOINT ["/opt/axon/container/entrypoint.sh"]
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "/opt/axon/container/healthcheck.py"]
