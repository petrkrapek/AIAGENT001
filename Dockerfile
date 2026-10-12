ARG BASE_IMAGE=python:3.13-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AIAGENT_PROFILE=/config/profil.md \
    HOME=/tmp

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && rm -rf /app/src /app/build

# Unprivileged user. All data is stored in Airtable, the container keeps no state.
RUN useradd --uid 1000 --no-create-home agent
USER agent
WORKDIR /tmp

ENTRYPOINT ["aiagent"]
