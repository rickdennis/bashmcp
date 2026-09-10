# bashmcp sandbox image for AgentCore Runtime (protocol HTTP). Build context: agentcore/sandbox.
#   docker buildx build --platform linux/arm64 -t <ecr>/bashmcp/sandbox:<tag> --push .
# Everything a user might need is baked in here: only /mnt/workspace survives a pause,
# so apt/pip installs done at runtime are lost when the microVM stops.
FROM artifactory.stoneridgeam.com/dockerhub-remote/library/ubuntu:24.04

ARG PYPI_INDEX=https://artifactory.stoneridgeam.com/artifactory/api/pypi/pypi-am-shared-noenv-virtual/simple
ARG NPM_REGISTRY=https://artifactory.stoneridgeam.com/artifactory/api/npm/npm-am-shared-noenv-virtual/
ENV DEBIAN_FRONTEND=noninteractive \
    PIP_INDEX_URL=${PYPI_INDEX} \
    UV_DEFAULT_INDEX=${PYPI_INDEX} \
    NPM_CONFIG_REGISTRY=${NPM_REGISTRY} \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl wget git jq unzip zip tar xz-utils less vim-tiny nano \
        procps psmisc iproute2 iputils-ping dnsutils openssh-client rsync sudo tmux tree \
        python3 python3-venv python3-pip python3-dev build-essential pkg-config \
        nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Isolated venv for the AgentCore entrypoint so user pip installs never break it.
COPY requirements.txt /opt/agentcore/requirements.txt
RUN python3 -m venv /opt/agentcore/venv \
    && /opt/agentcore/venv/bin/pip install --no-cache-dir -r /opt/agentcore/requirements.txt \
    && /opt/agentcore/venv/bin/pip install --no-cache-dir "uv==0.11.29" \
    && ln -s /opt/agentcore/venv/bin/uv /usr/local/bin/uv \
    && ln -s /opt/agentcore/venv/bin/uvx /usr/local/bin/uvx

COPY entrypoint.py /opt/agentcore/entrypoint.py

WORKDIR /root
EXPOSE 8080
CMD ["/opt/agentcore/venv/bin/python", "/opt/agentcore/entrypoint.py"]
