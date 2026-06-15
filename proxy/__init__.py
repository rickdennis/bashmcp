"""Front-door MCP router for the Firecracker bash-MCP fleet.

The router terminates MCP (owns the protocol session + the bash_exec tool),
places each session on a node-agent, records the binding as a Session CR, and
forwards command execution to the pinned node over plain HTTP. It is
leader-elected via the Kubernetes Lease API; only the leader reports Ready, so
the Service routes traffic to a single active replica at a time.
"""
