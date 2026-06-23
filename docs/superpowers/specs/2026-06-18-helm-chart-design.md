# fc-mcp Helm chart — design

**Date:** 2026-06-18
**Status:** Design (pending review)
**Companion:** `2026-06-18-keda-node-autoscaling-plan.md` (the autoscaling tier this chart renders)

## Purpose

Package the fc-mcp HA stack as a single Helm 3 chart so it installs with one
`helm install` on EKS, replacing the hand-applied `kubernetes/*.yaml` + `deploy/crds/`.
Full-stack scope (chosen): CRDs, namespace, `fc-local` StorageClass, RBAC, router
Deployment+Service, node-agent StatefulSet, headless Service, PDBs — **plus** the KEDA /
kube-state-metrics / Karpenter autoscaling tier, all gated behind `autoscaling.*` and
off by default.

The legacy single-host `deployment.yaml` is **excluded** (it defines its own namespace,
a `fast-ssd` PVC, and an `MCP_AUTH_TOKEN` Secret — not part of the HA topology).

## Chart layout

```
fc-mcp/
  Chart.yaml
  values.yaml
  values.schema.json
  .helmignore
  README.md
  crds/                                  # Helm 3 install-only (see CRD strategy)
    nodeagents.fcmcp.io.yaml
    sessions.fcmcp.io.yaml
  templates/
    _helpers.tpl
    NOTES.txt
    namespace.yaml                       # only when namespace.create=true
    serviceaccount-node-agent.yaml
    serviceaccount-router.yaml
    rbac-node-agent.yaml                 # Role + RoleBinding (namespaced)
    rbac-router.yaml                     # Role + RoleBinding (namespaced)
    storageclass-local.yaml              # cluster-scoped (release-prefixed name)
    node-agent-statefulset.yaml
    node-agent-headless-service.yaml
    router-deployment.yaml
    router-service.yaml
    pdb-node-agent.yaml
    pdb-router.yaml
    autoscaling/                         # all gated behind autoscaling.*
      keda-scaledobject.yaml
      ksm-customresourcestate-configmap.yaml
      ksm-clusterrole.yaml               # release-prefixed
      ksm-clusterrolebinding.yaml        # release-prefixed
      karpenter-nodepool.yaml            # release-prefixed
      karpenter-ec2nodeclass.yaml        # release-prefixed
    seeding/                             # only when seeding.mode=daemonset
      seed-daemonset.yaml
      seed-serviceaccount.yaml
      seed-rbac.yaml
    tests/
      test-router-ready.yaml             # helm test: router /readyz reachable
  ci/
    full-stack-values.yaml
    autoscaling-values.yaml
```

## Values structure (top level)

```yaml
image:        { repository, tag (required, no :latest), pullPolicy }
imagePullSecrets: []
namespace:    { create: false }          # prefer `helm install --create-namespace`
service:      { port: 8080 }             # ONE port drives everything
commonLabels/commonAnnotations: {}

nodeAgent:    # StatefulSet: privileged, hostNetwork, one-per-node
  replicas, podManagementPolicy, updateStrategy: OnDelete,
  terminationGracePeriodSeconds: 120,
  nodeSelector{fc-mcp: "true"}, taint, tolerations, extraAffinity,
  resources, fcBaseDir, maxVms (LOCKED 32 — see constraints),
  idlePauseSeconds, idleCheckInterval, fcBinary, extraEnv,
  networkSetup{enabled}, serviceAccount{create,name,annotations(IRSA)},
  probes{startup,liveness,readiness}, preStop{enabled},
  storage{size, className}, hostPaths{kvm, devNetTun}, seedMountType
storageClass: { create, name, provisioner, volumeBindingMode, reclaimPolicy }
router:       { replicas, command, resources, nodePort, lease, heartbeatTimeout,
                extraEnv, probes, serviceAccount, service{type,annotations},
                sessionAffinity: ClientIP }
ingress:      { enabled: false, ... }
podDisruptionBudget: { router{minAvailable:1}, nodeAgent{maxUnavailable:1} }
s3:           { bucket: "", prefix, region }     # empty bucket = archival off
seeding:      { enabled: false, mode: none|ami|daemonset, seedHostPath, daemonset{...} }
autoscaling:
  keda:       { enabled:false, minNodes:3, maxNodes:12, pollingInterval,
                prometheusAddress, targets{tapsPerNode:24, runningVmsPerNode:20,
                storageBytesPerNode}, behavior{scaleUp}, queries{...overrides} }
  ksmCustomResourceState: { enabled:false, metricNamePrefix, serviceAccount{name,namespace} }
  karpenter:  { enabled:false, instanceTypes[metal], capacityType[on-demand],
                amiFamily, amiSelectorTerms, subnet/sgSelectorTerms, role(req), disruption, limits }
```
Defaults reproduce the current hand-rolled manifests exactly, so a default install ==
today's HA deployment. The full annotated `values.yaml` is authored in implementation;
the load-bearing parts and their guards are specified below.

## Key design decisions

**Single image, two roles.** One `fc-bash-mcp` image. Node-agent uses the default
entrypoint (`start.sh` → `server.py`); router overrides `command` to
`uv run python -m proxy.router` (bypassing `start.sh`'s KVM preflight, which would exit
on the non-KVM router pod). `router.command`'s `--port` is **templated from
`service.port`**, not a literal.

**CRDs via `crds/` (install-only).** Both `fcmcp.io` CRDs ship in `crds/`. Helm installs
them before the release and **never upgrades or deletes them**. This is deliberate: the
CRDs are a hardcoded contract (`fcmcp.io/v1alpha1`, referenced by `server.py`,
`proxy/k8s.py`, and RBAC), the CRs are namespaced routing state, and templating them
would let `helm uninstall` cascade-delete every `Session`/`NodeAgent` in the cluster —
destroying the router's routing table. Cost: CRD schema changes need a manual
`kubectl apply -f fc-mcp/crds/` before a chart upgrade (loudly documented in NOTES).
KEDA `ScaledObject`, KSM `CustomResourceStateMetrics` ConfigMap, and Karpenter
`NodePool`/`EC2NodeClass` are **instances** of *other* projects' CRDs → they live in
`templates/` (gated, GC'd with the release), not `crds/`.

**Leader-only-Ready router.** The router Deployment keeps its Lease-elected,
leader-only-`Ready` behavior; the ClusterIP Service normally has exactly one endpoint.
The chart adds **`sessionAffinity: ClientIP`** to the router Service so that during a
leader-failover window with two transiently-`Ready` endpoints, a stateful MCP session
(`stateless_http=False` is load-bearing) isn't round-robined to a replica that never
initialized it (→ 404). Flipping the MCP transport flags is documented as a design fork.

**Seeding (the autoscaling prerequisite).** An autoscaled node arrives empty; the
node-agent needs `/opt/fc-seed` (kernel + rootfs + `vm_ssh_key`) or every VM create
fails "Kernel not found". `seeding.mode`:
- `ami` (recommended prod): seed baked into the node AMI; chart renders nothing, NOTES
  documents the bake.
- `daemonset` (bootstrap/iteration): privileged DaemonSet on the pool `aws s3 sync`s the
  seed from S3 to the host `seedHostPath`, chmods the key `0400`, then idles. Gets its
  own SA (IRSA for `s3:Get/ListBucket`). The generous startup probe (30×10s) covers the
  race.
- `none` (default): assume `/opt/fc-seed` present (kind/manual).

**Autoscaling values contract** follows the KEDA plan §5–9 verbatim: three Prometheus
triggers (taps / running-VMs / storage), `scaleDown` hardcoded `Disabled`, KEDA owns the
StatefulSet `replicas` when enabled (chart **omits** `spec.replicas` entirely — never
`null`), KSM `CustomResourceState` maps NodeAgent CR fields to `fc_nodeagent_*` gauges,
Karpenter NodePool/EC2NodeClass for the `fc-mcp` pool with `do-not-disrupt` on the pods.

## Correctness constraints (must be enforced, from adversarial review)

These are non-negotiable; several would silently break a real deploy.

1. **`maxVms` is LOCKED to 32, not a capacity knob.** Real capacity is hardcoded
   `SLOT_MIN=2`/`SLOT_MAX=33` in `server.py` and `seq 0 31` in `setup-network.sh`;
   `FC_MAX_VMS` only sets the value *advertised* in the NodeAgent CR (the router's
   placement view). Setting it `>32` makes the router over-place → "node at capacity".
   → values comment says it's locked to the in-code/`setup-network.sh` constant, and a
   `_helpers.tpl` assertion **fails the render if `maxVms != 32`**. Real tunability is a
   *code* change (parameterize `SLOT_MAX` + the seq from one env) that must land first.

2. **`app.kubernetes.io/*` labels NEVER in any selector.** `spec.selector` on
   StatefulSet/Deployment and `Service`/`PDB` selectors stay **exactly** the legacy bare
   `app:` set (`app: fc-node-agent` / `app: fc-mcp-router`); the richer labels are
   non-selector metadata only. Otherwise `helm upgrade` over the existing kubectl-applied
   manifests fails on the immutable `spec.selector`. A CI render test asserts
   `matchLabels == {app: ...}` only.

3. **`seeding.mode=none` + `autoscaling.keda.enabled` HARD-FAILS the render** (not a
   warning). An unseeded autoscaled node makes every VM create fail. Also: the
   `vm_ssh_key` hostPath is `type: File` (must pre-exist) → on an unseeded node the pod
   **CrashLoops**, not just goes NotReady. `seedMountType` is configurable; NOTES states
   the crashloop-until-seeded behavior.

4. **Immutable `volumeClaimTemplates`.** `storage.size`/`className` are "set once":
   changing them fails `helm upgrade`. values comments + a README runbook (orphan-delete
   the STS, recreate) make this loud; the chart cannot make it a live upgrade.

5. **Cluster-scoped objects are release-prefixed.** StorageClass, KSM
   ClusterRole/ClusterRoleBinding, and Karpenter NodePool/EC2NodeClass use a
   `{{ include "fc-mcp.fullname" . }}-` prefix so two releases don't collide. (The
   `storageClass.name` the volumeClaimTemplate references is resolved via one helper so
   both sides stay equal.)

6. **Prereq CRDs gated.** KEDA / Karpenter / KSM are **not** installed by this chart.
   Each autoscaling template is gated on both its `enabled` flag **and**
   `.Capabilities.APIVersions.Has` for the relevant CRD, so `helm install` with a feature
   on doesn't fail with "no matches for kind ScaledObject" when the operator hasn't
   installed the prerequisite. NOTES lists the prerequisites + install order.

7. **Single port.** `service.port` interpolates into containerPort, `MCP_PORT`, router
   `--port`, all probe `httpGet.port`, the preStop `/drain` URL, `FC_MCP_NODE_PORT`, and
   both Services. A CI test asserts no literal `8080` remains when `service.port` differs.

8. **S3 auth under hostNetwork.** Prefer IRSA (projected token, hop-limit independent)
   over the node instance role; the IRSA `role-arn` is a first-class value on the
   node-agent SA. NOTES warns: relying on the instance role needs IMDSv2 hop-limit ≥ 2
   (the gotcha we already hit).

9. **Resource adoption.** A fresh `helm install` into a namespace that already holds the
   kubectl-applied objects fails ("exists and cannot be imported"). README provides a
   one-time adoption path (annotate/label existing objects with
   `meta.helm.sh/release-name` + `managed-by=Helm`) or mandates a clean namespace.

## NOTES.txt (post-install guidance)

Endpoint (point Claude Code at the **router** Service/ingress, never a node; auth/TLS at
the gateway in front); CRD upgrade caveat (`crds/` not upgraded by Helm); per-feature
prerequisites (KVM nodes; KEDA+Prometheus+KSM; Karpenter role/selectors); the
`seeding.mode=none`+autoscaling warning; the **scale-up-only / dead-node drain runbook**;
the **OnDelete rollout** caveat (after `helm upgrade`, node-agent pods don't auto-restart
— `kubectl delete pod` one at a time, honoring `maxUnavailable: 1`, letting `/drain` +
re-Ready complete between each); PVC lifecycle (uninstall leaves `fc-data-*`, reinstall
re-adopts them; `kubectl delete pvc -l app=fc-node-agent` to purge); verify commands.

## values.schema.json

Enforces: `image.tag` required, non-empty, pattern excludes `latest`; `maxVms` `const 32`;
`seeding.mode` enum `[none,ami,daemonset]`; `karpenter.role` required when
`karpenter.enabled`. Cross-field rules JSON Schema can't express (`keda.minNodes ≤
maxNodes`, `keda.enabled ⇒ seeding.mode ≠ none`) are enforced by `_helpers.tpl` `fail`.

## Testing

- **`helm lint`** + **`helm template`** against `ci/full-stack-values.yaml` and
  `ci/autoscaling-values.yaml` (asserts the gated paths render).
- **Render assertions** (the constraints above): `matchLabels` are bare `app:` only;
  no literal `8080` when `service.port` is overridden; render **fails** on `maxVms != 32`
  and on `keda.enabled && seeding.mode==none`; `spec.replicas` absent when `keda.enabled`.
- **`helm test`**: a Job hitting the router `/readyz`.
- **kind**: install with autoscaling off → matches the current validated stack; install
  with `keda.enabled` + a stub Prometheus → ScaledObject + HPA appear (node provisioning
  stops at Pending, as expected without a cloud pool).
- **Cloud (EKS)**: end-to-end with Karpenter + seeding — Pending pod → new node → seeded
  → `freeTaps` rises.

## Open questions (need your input)

1. **Karpenter specifics:** instance types must be **bare-metal / nested-virt-capable**
   for KVM (defaults `m5.metal`/`m6i.metal` are placeholders) — what's the real type? And
   the node IAM `role`, subnet/SG `karpenter.sh/discovery` tags?
2. **Image registry:** EKS almost certainly needs an ECR URI
   (`<acct>.dkr.ecr.<region>.amazonaws.com/fc-bash-mcp`) + a tagging scheme as the default
   — is there a canonical repo? `imagePullSecrets` or node-IAM+ECR?
3. **Seeding for prod:** AMI bake vs S3 DaemonSet — which is committed? If S3, the seed
   bucket/prefix layout and seeder IRSA role. (`vm_ssh_key` over S3 is sensitive —
   AMI-only instead?)
4. **`runningVmsPerNode` threshold (20):** depends on the real instance RAM ÷ VM mem —
   set once the instance type is known.
5. **StorageClass provisioner on EKS:** default `rancher.io/local-path` is from kind. EKS
   local PVs usually mean the static `kubernetes.io/no-provisioner` (pre-created PVs on
   instance store) or a CSI — which is the target?
6. **S3 archival default:** on by default in prod (it's the only thing that lets paused
   VMs survive node loss)? With which IRSA role on the node-agent SA?
7. **KSM integration:** is kube-state-metrics managed via the kube-prometheus-stack
   umbrella (pass values) or standalone (the ConfigMap + manual
   `--custom-resource-state-config-file` arg)?

## Critical files
- `fc-mcp/` chart (new) — templates, `values.yaml`, `values.schema.json`, `crds/`, NOTES,
  helpers, `ci/` values, `templates/tests/`.
- Source manifests templated from: `kubernetes/{statefulset,router-deployment,
  router-service,headless-service,rbac,pdb,storageclass-local}.yaml`, `deploy/crds/*`.
- `CLAUDE.md` — document `helm install` as the deploy path; the `maxVms`-locked-to-32
  reality; the OnDelete rollout + immutable-field runbooks.
- (Possible follow-up, out of scope) `server.py`/`setup-network.sh` — parameterize
  `SLOT_MAX` + tap count from one env if `maxVms` is ever to become truly tunable.
```
