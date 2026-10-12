# Pinned by digest for reproducible builds; bump deliberately (e.g. via Dependabot).
ARG BASE_IMAGE=python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

WORKDIR /app

# Dependencies first, from the hashed lock file: this layer is reused until the lock changes.
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps . && rm -rf /app/src /app/build

# Unprivileged user. Everything is stored in Airtable, the container keeps no state.
RUN useradd --uid 1000 --no-create-home agent
USER agent
WORKDIR /tmp

ENTRYPOINT ["aiagent"]
