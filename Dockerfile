# Runs the MCP+REST server. Requires --privileged and /dev/kvm for Firecracker.
#
# Usage:
#   docker build -t fc-bash-mcp .
#   docker run --privileged \
#     --device /dev/kvm \
#     -v /opt/fc-mcp:/opt/fc-mcp \
#     -p 8080:8080 \
#     fc-bash-mcp

FROM ubuntu:22.04

RUN apt-get update && apt-get install -y \
    python3 python3-pip \
    openssh-client \
    curl wget \
    iproute2 iptables nftables \
    e2fsprogs \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install Firecracker
ARG FC_VERSION=v1.10.1
RUN ARCH=$(uname -m) && \
    curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz" \
    | tar -xz && \
    mv "release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" /usr/bin/firecracker && \
    mv "release-${FC_VERSION}-${ARCH}/jailer-${FC_VERSION}-${ARCH}" /usr/bin/jailer && \
    chmod +x /usr/bin/firecracker /usr/bin/jailer && \
    rm -rf release-*

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY server.py ./
COPY scripts/start.sh scripts/setup-network.sh ./
COPY proxy/ ./proxy/
# fc-egress (the egress broker sidecar/process). Cluster hosts are linux/amd64.
COPY bin/fc-egress-amd64 /usr/local/bin/fc-egress
RUN chmod +x start.sh setup-network.sh /usr/local/bin/fc-egress

ENV FC_BASE_DIR=/opt/fc-mcp
ENV MCP_PORT=8080
ENV MCP_HOST=0.0.0.0

EXPOSE 8080

# One image, two roles:
#   node-agent (default): ./start.sh -> server.py  (needs KVM + privileged)
#   router:    override command -> ["uv","run","python","-m","proxy.router","--port","8080"]
ENTRYPOINT ["./start.sh"]
