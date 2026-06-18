# Plan — KEDA-driven node-fleet scale-up for fc-mcp

**Date:** 2026-06-18
**Scope:** Scale **up** the `fc-node-agent` fleet (and therefore the `fc-mcp` node pool)
when capacity runs low: **too few free taps**, **too little local storage**, or **too
many running VMs**. Scale-down is intentionally NOT automated (see §2).

## 1. The two-layer reality (KEDA does not add nodes by itself)

The hard `podAntiAffinity` puts exactly one node-agent per node, so "add a node-agent
replica" == "add a node". That takes two cooperating controllers:

1. **KEDA** watches capacity metrics and raises the **`fc-node-agent` StatefulSet
   `replicas`** (via the HPA it manages).
2. The new ordinal pod cannot schedule (anti-affinity needs a node with no node-agent;
   it also needs the `fc-mcp` taint/label and a fresh local PV). It goes **Pending**.
3. **Cluster Autoscaler / Karpenter** sees the Pending pod and **provisions a new
   `fc-mcp` node**. The pod schedules, the local PV binds, the node-agent boots,
   reconciles, publishes its NodeAgent CR, and `freeTaps` rises fleet-wide.

KEDA is the *demand sensor + replica setter*; the cluster autoscaler is the *node
provider*. Both are required. This plan specifies both.

## 2. Scale-UP only — by design

VMs are **pets pinned to a node**; losing a node loses its running VMs (paused ones
survive only via the preStop `/drain` snapshot on the local PV, and via S3 archival if
enabled). Therefore:

- KEDA `ScaledObject` sets `behavior.scaleDown.selectPolicy: Disabled` → it will
  **only ever raise** the replica count.
- The autoscaler's scale-**down** for this pool stays disabled (existing requirement:
  `cluster-autoscaler.kubernetes.io/scale-down-disabled=true`; Karpenter equivalent:
  `karpenter.sh/do-not-disrupt: "true"` on the node-agent pods).
- Removing a node remains the deliberate **dead-node / drain runbook** (drain → confirm
  S3 archives → delete StatefulSet ordinal's PVC → shrink pool). Out of scope here.

This matches the directive ("scaling **up** … when it needs it") and the app's pet
semantics.

## 3. Signals → metrics → sources

Three **independent** per-node constraints, each its own KEDA trigger:

| Constraint | Why it's distinct | Metric (fleet sum) | Source — **no app change** |
|---|---|---|---|
| **Taps/slots** (32/node hard cap) | A slot is held by every VM, *including paused ones* (slot released only on destroy) | `sum(maxVms) - sum(freeTaps)` = used taps | NodeAgent CR `spec.maxVms`, `status.freeTaps` via **kube-state-metrics CustomResourceState** |
| **Running VMs** (host RAM/CPU) | Only *running* VMs consume host RAM (≈512 MB each) + the resume-spin CPU; paused ones don't | `sum(runningVmCount)` | NodeAgent CR `status.runningVmCount` via KSM |
| **Local storage** (overlays+snapshots on the 100 Gi local PV) | Disk fills independently of slot/RAM | `sum(kubelet_volume_stats_used_bytes{pvc=~"fc-data-.*"})` | **kubelet** already exports this; no KSM/app change |

`server.py` already publishes `freeTaps`, `runningVmCount`, `maxVms`, `phase`,
`heartbeatTime` on the NodeAgent CR (server.py:832-848), so taps + running-VM signals
need only a KSM mapping. Storage comes free from kubelet. (Optional later: a node-agent
`/metrics` endpoint for richer gauges — committed VM memory bytes, per-status counts —
but **not required** for v1.)

## 4. How KEDA turns a signal into a replica count

KEDA's Prometheus scaler reports each query to the HPA as an **AverageValue** target,
so per trigger: `desiredReplicas = ceil(fleetMetric / threshold)`, and KEDA takes the
**max across triggers**. So the trigger metric must be a **demand** value (monotonic
with load) and the `threshold` is **per-node capacity × target-utilization** — which
bakes in headroom so a node is provisioned *before* the fleet is full.

- Taps: `threshold = 24` (= 32 × 0.75). 3 nodes saturate at 72 used taps → 4th node
  is requested as usage crosses 72.
- Running VMs: `threshold = 20` (tune to node RAM ÷ 512 MB × ~0.7).
- Storage: `threshold = 80530636800` bytes (= 100 Gi × 0.75).

Headroom matters because provisioning is **minutes** (ASG scale-up + node join + image
pull + reconcile + `/ready`). Target 70–75 %, not 95 %.

## 5. The KEDA ScaledObject (lives in the Helm chart — see §8)

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: fc-node-agent
  namespace: fc-mcp
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: StatefulSet
    name: fc-node-agent
  pollingInterval: 30
  minReplicaCount: 3          # floor = baseline pool size
  maxReplicaCount: 12         # cost/cluster cap; alert when hit (capacity exhausted)
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:
          stabilizationWindowSeconds: 60
          policies: [{ type: Pods, value: 2, periodSeconds: 120 }]  # +2 nodes/2min max
        scaleDown:
          selectPolicy: Disabled            # SCALE-UP ONLY (pet VMs)
  triggers:
    - type: prometheus       # taps
      metadata:
        serverAddress: http://prometheus-server.monitoring.svc:80
        query: sum(fc_nodeagent_max_vms) - sum(fc_nodeagent_free_taps)
        threshold: "24"
    - type: prometheus       # running VMs (host RAM/CPU)
      metadata:
        serverAddress: http://prometheus-server.monitoring.svc:80
        query: sum(fc_nodeagent_running_vms)
        threshold: "20"
    - type: prometheus       # local storage
      metadata:
        serverAddress: http://prometheus-server.monitoring.svc:80
        query: sum(kubelet_volume_stats_used_bytes{namespace="fc-mcp",persistentvolumeclaim=~"fc-data-.*"})
        threshold: "80530636800"
```

## 6. Metric pipeline

**kube-state-metrics CustomResourceState** (maps NodeAgent CR fields → Prometheus):

```yaml
# values for kube-state-metrics: extraArgs/custom resource state config
kind: CustomResourceStateMetrics
spec:
  resources:
    - groupVersionKind: {group: fcmcp.io, version: v1alpha1, kind: NodeAgent}
      metricNamePrefix: fc_nodeagent
      labelsFromPath: { node: [spec, nodeName] }
      metrics:
        - name: free_taps
          each: {type: Gauge, gauge: {path: [status, freeTaps]}}
        - name: running_vms
          each: {type: Gauge, gauge: {path: [status, runningVmCount]}}
        - name: max_vms
          each: {type: Gauge, gauge: {path: [spec, maxVms]}}
```
KSM's ServiceAccount needs `get/list/watch` on `fcmcp.io/nodeagents`. Prometheus must
scrape KSM and kubelet (standard kube-prometheus-stack defaults already do).

## 7. Node-provisioning layer (one of these)

**A. Cluster Autoscaler** on the `fc-mcp` managed nodegroup/ASG. Tag the ASG so CA can
predict a new node satisfies the tainted/labeled Pending pod:
```
k8s.io/cluster-autoscaler/enabled
k8s.io/cluster-autoscaler/<cluster-name>
k8s.io/cluster-autoscaler/node-template/label/fc-mcp = "true"
k8s.io/cluster-autoscaler/node-template/taint/fc-mcp = "true:NoSchedule"
```
ASG `min = minReplicaCount`, `max = maxReplicaCount`. Keep scale-down disabled for the
pool (§2).

**B. Karpenter** NodePool: `requirements`/`labels` set `fc-mcp: "true"`, `taints:
[{key: fc-mcp, value: "true", effect: NoSchedule}]`, `disruption.consolidationPolicy:
WhenEmpty` + `expireAfter: Never`; rely on `karpenter.sh/do-not-disrupt` on the pods so
nodes with VMs are never reclaimed.

## 8. Where it lives (coordinate with the Helm-chart fork)

These are **deploy artifacts**, not app code. They belong in the Helm chart the other
fork is building, gated behind a values flag:

```yaml
autoscaling:
  keda:
    enabled: false
    minNodes: 3
    maxNodes: 12
    prometheusAddress: http://prometheus-server.monitoring.svc:80
    targets: { tapsPerNode: 24, runningVmsPerNode: 20, storageBytesPerNode: 80530636800 }
  ksmCustomResourceState: true   # render the NodeAgent CRS mapping
```
Chart renders: the `ScaledObject`, the KSM CRS config + RBAC, and (templated) the CA ASG
tags / Karpenter NodePool. KEDA itself and Prometheus/KSM are cluster prerequisites
(documented as chart dependencies, not installed by this chart).

## 9. Hard prerequisite — seed new nodes

An autoscaled node arrives **empty**; the node-agent needs `/opt/fc-seed`
(kernel + rootfs + `vm_ssh_key`) or it boots but fails every VM create with
"Kernel not found" (the exact failure already seen in kind). Autoscaling is only useful
if new nodes are seeded automatically:
- **Preferred:** bake the seed into the `fc-mcp` node AMI/image.
- **Alternative:** a privileged seeding DaemonSet on the pool that pulls
  kernel/rootfs/key from S3 to `/opt/fc-seed` on node join (the StatefulSet's generous
  startupProbe — 30×10 s — gives it time). 

**This must be solved before KEDA scale-up yields usable capacity.** Flagged as the #1
dependency.

## 10. End-to-end scale-up flow

1. Load rises → used taps / running VMs / used storage climb.
2. KSM + kubelet expose them; Prometheus scrapes.
3. KEDA computes `max(ceil(used_taps/24), ceil(running/20), ceil(used_bytes/80.5e9))`
   > current → bumps `fc-node-agent` replicas.
4. New ordinal Pending (anti-affinity + taint + new `fc-data-N` PVC, WaitForFirstConsumer).
5. CA/Karpenter provisions a tainted/labeled `fc-mcp` node.
6. Node join → seed present (§9) → pod schedules → PV binds → node-agent boots →
   reconcile → NodeAgent CR published → `/ready`.
7. Fleet `freeTaps` rises; the router places new sessions on it automatically (it reads
   NodeAgent `freeTaps` — no router change needed).

## 11. Risks / gotchas

- **Provisioning latency (minutes):** mitigated by 70–75 % thresholds + `scaleUp`
  surge (+2 nodes/2 min). Tune for traffic shape.
- **maxReplicaCount hit = real exhaustion:** add a Prometheus alert on
  `keda_scaledobject_paused`/HPA `DesiredReplicas == maxReplicaCount` and on fleet
  `freeTaps == 0`.
- **Orphan PVCs on any future scale-down:** each scale-up creates `fc-data-N`; removal
  is the manual runbook (§2). Never let CA/Karpenter reclaim these nodes automatically.
- **Metric staleness:** if a node-agent stops heartbeating, its CR `freeTaps` goes
  stale; pair with the router's liveness handling. KEDA reacting to a stale-but-present
  CR is acceptable for scale-up (over-provisioning is safe; under is not).
- **Flapping:** impossible to thrash nodes down because scale-down is disabled; worst
  case is over-provisioning, bounded by `maxReplicaCount`.

## 12. Validation

- **Logic/threshold:** unit-check the PromQL → desired-replica math against recorded
  KSM output (no cluster).
- **kind (single host):** KEDA + KSM CRS + a stub Prometheus can be installed; drive
  `fcctl`/the router to create VMs until `used_taps` crosses 24 and confirm the
  `ScaledObject` raises the StatefulSet to 4 replicas and the 4th pod goes **Pending**
  (kind has no ASG, so node provisioning stops there — that's the expected limit; CA/
  Karpenter is validated only on a real cloud pool).
- **Cloud:** end-to-end on the `fc-mcp` nodegroup with CA — confirm Pending → new node
  → seeded → `freeTaps` rises.

## 13. Open questions
- Cluster Autoscaler vs Karpenter for the `fc-mcp` pool? (Plan supports either.)
- Seed delivery: AMI bake vs seeding DaemonSet (§9) — pick before enabling.
- Right per-node `runningVmsPerNode` threshold = f(node instance RAM, default VM mem).
- Should a 4th "session pressure" trigger (router `Pending` sessions) exist, or are the
  three resource triggers sufficient? (Resource triggers are the leading indicators;
  session-pressure would be a lagging backstop.)
```

## Critical files (when implemented)
- Helm chart (other fork): `ScaledObject`, KSM CustomResourceState + RBAC, CA tags /
  Karpenter NodePool, `values.autoscaling.*`.
- `kubernetes/` docs / `CLAUDE.md`: document the scale-up tier + the scale-down-only-by-
  runbook rule + the seeding prerequisite.
- No `server.py` change required for v1 (metrics already on the NodeAgent CR + kubelet).
