"""Minimal AgentCore Runtime entrypoint for the sandbox image.

AgentCore requires an HTTP server on 0.0.0.0:8080 answering GET /ping and POST /invocations.
The sandbox never does agent reasoning; all work arrives via InvokeAgentRuntimeCommand, which
runs shell commands in this same container as root. This process just keeps the session alive.
"""
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload):
    return {
        "status": "ok",
        "message": "bashmcp sandbox. Use InvokeAgentRuntimeCommand to run commands; /mnt/workspace persists.",
        "echo": payload,
    }


if __name__ == "__main__":
    app.run()
