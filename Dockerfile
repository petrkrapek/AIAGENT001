ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AIAGENT_WORKSPACE=/data/workspace \
    AIAGENT_PROFILE=/data/profil.md \
    HOME=/tmp

WORKDIR /app

# Dependencies first so code changes don't invalidate this layer.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf /app/src /app/build

# Unprivileged user; compose can override the UID/GID to match the host.
RUN useradd --uid 1000 --create-home --home-dir /home/agent agent \
    && mkdir -p /data/workspace \
    && chown -R agent:agent /data
USER agent
WORKDIR /data

ENTRYPOINT ["aiagent"]
