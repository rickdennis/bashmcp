# bashmcp broker for AgentCore Runtime (protocol MCP). Build context: agentcore/ (parent dir).
#   docker buildx build --platform linux/arm64 -f broker/Dockerfile -t <ecr>/bashmcp/broker:<tag> --push .
FROM artifactory.stoneridgeam.com/dockerhub-remote/library/python:3.12-slim

ARG PYPI_INDEX=https://artifactory.stoneridgeam.com/artifactory/api/pypi/pypi-am-shared-noenv-virtual/simple
ENV PIP_INDEX_URL=${PYPI_INDEX} \
    UV_DEFAULT_INDEX=${PYPI_INDEX} \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir "uv==0.11.29"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY broker ./broker

RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app
USER 1000
ENV PATH=/opt/venv/bin:${PATH}
EXPOSE 8000
CMD ["python", "-m", "broker.app"]
