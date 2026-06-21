# archive/

Retired, **unmaintained** artifacts kept for reference only. Nothing here is part of the current
build or deploy path.

- **`deployment.yaml`** — the original single-host Kubernetes manifest (one `privileged`,
  `hostNetwork` pod running `server.py` directly). Superseded by the HA topology in
  `kubernetes/` + `deploy/crds/` (a leader-elected router + a node-agent StatefulSet). Its paths
  are stale (e.g. the `setup-network.sh` initContainer command) and it is no longer applied or
  tested — consult it only as an example of the simplest single-node deployment.
