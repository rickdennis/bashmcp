# Dockerfile
# Runs the MCP server itself (NOT the VMs — those run directly on the host via Firecracker).
# The container must be run with --privileged and /dev/kvm mounted for Firecracker to work.
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
    iproute2 iptables \
    e2fsprogs \
    && rm -rf /var/lib/apt/lists/*

# Install Firecracker
ARG FC_VERSION=v1.10.1
RUN ARCH=$(uname -m) && \
    curl -fsSL "https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz" \
    | tar -xz --strip-components=1 && \
    mv "release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" /usr/bin/firecracker && \
    mv "release-${FC_VERSION}-${ARCH}/jailer-${FC_VERSION}-${ARCH}" /usr/bin/jailer && \
    chmod +x /usr/bin/firecracker /usr/bin/jailer && \
    rm -rf release-*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
RUN chmod +x scripts/*.sh

ENV FC_BASE_DIR=/opt/fc-mcp
ENV MCP_PORT=8080
ENV MCP_HOST=0.0.0.0

EXPOSE 8080

ENTRYPOINT ["scripts/start.sh"]
