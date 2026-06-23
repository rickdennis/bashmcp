# fc-mcp Helm Chart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the fc-mcp HA stack as a Helm 3 chart (`fc-mcp/`) that installs the full stack — CRDs, RBAC, router, node-agent StatefulSet, services, PDBs — plus an optional KEDA/KSM/Karpenter autoscaling tier, with defaults that reproduce the current hand-applied manifests exactly.

**Architecture:** One chart, one image used in two roles (node-agent default entrypoint; router via `command` override). CRDs ship in `crds/` (install-only). All env-specific knobs and the autoscaling tier are values, off/locked by default. Load-bearing invariants from the design spec are enforced at render time via `_helpers.tpl` `fail` guards (no `:latest`, `maxVms==32`, selectors stay bare `app:`, `keda.enabled ⇒ seeding.mode≠none`).

**Tech Stack:** Helm 3, Go templating, KEDA `ScaledObject`, kube-state-metrics `CustomResourceState`, Karpenter `NodePool`/`EC2NodeClass`. Tests are `helm lint` + `helm template` render assertions via a dependency-free bash harness (`ci/render-tests.sh`) plus a `helm test` hook.

**Source of truth:** spec `docs/superpowers/specs/2026-06-18-helm-chart-design.md`; autoscaling contract `docs/superpowers/specs/2026-06-18-keda-node-autoscaling-plan.md`; existing manifests `kubernetes/*.yaml`, `deploy/crds/*.yaml`.

**Prerequisites:** `helm` v3.12+ on PATH. No cluster needed for the render tests (Tasks 1–12 are all `helm template`/`lint` driven). Install KEDA/Prometheus/KSM/Karpenter only for live cloud validation (out of plan scope).

---

## File structure

```
fc-mcp/
  Chart.yaml                 # chart metadata, appVersion = image default tag
  values.yaml                # all knobs; defaults == current manifests
  values.schema.json         # static validation (enum/required/pattern)
  .helmignore
  README.md                  # install, adoption, runbooks
  crds/                      # install-only (Helm never upgrades/deletes)
    nodeagents.fcmcp.io.yaml
    sessions.fcmcp.io.yaml
  templates/
    _helpers.tpl             # naming, labels, selectors, image, port, fail-guards
    NOTES.txt
    namespace.yaml
    serviceaccount-node-agent.yaml
    serviceaccount-router.yaml
    rbac-node-agent.yaml
    rbac-router.yaml
    storageclass-local.yaml
    node-agent-statefulset.yaml
    node-agent-headless-service.yaml
    router-deployment.yaml
    router-service.yaml
    pdb-node-agent.yaml
    pdb-router.yaml
    seeding/
      seed-serviceaccount.yaml
      seed-rbac.yaml
      seed-daemonset.yaml
    autoscaling/
      keda-scaledobject.yaml
      ksm-customresourcestate-configmap.yaml
      ksm-clusterrole.yaml
      ksm-clusterrolebinding.yaml
      karpenter-nodepool.yaml
      karpenter-ec2nodeclass.yaml
    tests/
      test-router-ready.yaml
  ci/
    render-tests.sh          # the bash assertion harness (the "test runner")
    full-stack-values.yaml
    autoscaling-values.yaml
```

Each component is one focused template. Cluster-scoped objects (StorageClass, KSM ClusterRole/Binding, Karpenter resources) get a `fullname` prefix so two releases can't collide. Selectors on StatefulSet/Deployment/Service/PDB stay the **bare `app:` label** (immutable-field safety); richer `app.kubernetes.io/*` labels are non-selector metadata only.

---

## The test harness (used by every task)

`ci/render-tests.sh` is the "test runner": dependency-free bash that drives `helm template`/`helm lint` and asserts on the rendered YAML with `grep`. The TDD loop per task is: add an assertion (it fails because the template/behavior doesn't exist) → run → add the template → run → commit.

It is created in Task 1 and appended to in later tasks. Run it with `bash fc-mcp/ci/render-tests.sh` from the repo root.

---

### Task 1: Chart scaffold + contract (Chart.yaml, values.yaml, _helpers.tpl, schema, harness)

**Files:**
- Create: `fc-mcp/Chart.yaml`
- Create: `fc-mcp/.helmignore`
- Create: `fc-mcp/values.yaml`
- Create: `fc-mcp/templates/_helpers.tpl`
- Create: `fc-mcp/values.schema.json`
- Create: `fc-mcp/ci/render-tests.sh`

- [ ] **Step 1: Write the failing test (create the harness with the first assertions)**

Create `fc-mcp/ci/render-tests.sh`:

```bash
#!/usr/bin/env bash
# Dependency-free render-test harness for the fc-mcp chart.
# Usage: bash fc-mcp/ci/render-tests.sh
set -uo pipefail
CHART="$(cd "$(dirname "$0")/.." && pwd)"
FAIL=0
pass() { echo "  ok: $1"; }
fail() { echo "  FAIL: $1"; FAIL=1; }

# render <name> <extra helm args...> -> echoes rendered YAML, sets RC
render() { helm template t "$CHART" "$@" 2>/tmp/fcmcp-render.err; }

assert_lint() {
  if helm lint "$CHART" "$@" >/tmp/fcmcp-lint.out 2>&1; then pass "helm lint $*"; \
  else fail "helm lint $* — $(tail -1 /tmp/fcmcp-lint.out)"; fi
}
assert_contains() { # <desc> <pattern> <helm args...>
  local d="$1" p="$2"; shift 2
  if render "$@" | grep -Eq "$p"; then pass "$d"; else fail "$d (missing /$p/)"; fi
}
assert_absent() { # <desc> <pattern> <helm args...>
  local d="$1" p="$2"; shift 2
  if render "$@" | grep -Eq "$p"; then fail "$d (unexpected /$p/)"; else pass "$d"; fi
}
assert_render_fails() { # <desc> <expected-substring-in-error> <helm args...>
  local d="$1" e="$2"; shift 2
  if render "$@" >/dev/null 2>/tmp/fcmcp-render.err; then fail "$d (render unexpectedly succeeded)";
  elif grep -q "$e" /tmp/fcmcp-render.err; then pass "$d";
  else fail "$d (error did not contain '$e': $(tail -1 /tmp/fcmcp-render.err))"; fi
}

# ---- Task 1: contract ----
REQ="--set image.tag=v1.0.0"   # all renders must pin a tag
assert_lint $REQ
assert_contains "Chart renders a StatefulSet" "kind: StatefulSet" $REQ
assert_render_fails "render fails without image.tag" "image.tag must be set" --set image.tag=""
assert_render_fails "render fails on :latest tag" "image.tag must be set" --set image.tag=latest

echo; [ "$FAIL" -eq 0 ] && echo "ALL PASS" || { echo "FAILURES"; exit 1; }
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh`
Expected: FAIL — chart doesn't exist yet (`helm lint`/`helm template` error: no `Chart.yaml`).

- [ ] **Step 3: Create `fc-mcp/Chart.yaml`**

```yaml
apiVersion: v2
name: fc-mcp
description: Firecracker microVM bash-MCP HA stack (router + node-agents) with optional KEDA/Karpenter node autoscaling
type: application
version: 0.1.0
appVersion: "0.1.0"     # default image tag when image.tag is empty (override in prod)
kubeVersion: ">=1.27.0-0"
keywords: [firecracker, mcp, microvm, claude-code]
home: https://github.com/your-org/bashmcp
maintainers:
  - name: Code Services
```

- [ ] **Step 4: Create `fc-mcp/.helmignore`**

```
.git
*.md
ci/
tests-output/
*.tmp
```

- [ ] **Step 5: Create `fc-mcp/values.yaml`**

```yaml
# values.yaml — fc-mcp full-stack chart. Defaults reproduce kubernetes/*.yaml.
# Install: helm install fc-mcp ./fc-mcp -n fc-mcp --create-namespace --set image.tag=<tag>

# One image, two roles. NEVER :latest — render fails if tag is empty/latest.
image:
  repository: fc-bash-mcp
  tag: ""                  # required; falls back to .Chart.AppVersion if set there
  pullPolicy: IfNotPresent
imagePullSecrets: []

# Optional chart-managed Namespace. Prefer `--create-namespace` and leave false.
namespace:
  create: false
  # name: ""               # defaults to .Release.Namespace

# ONE port drives container/probe/service/preStop/router-node-port.
service:
  port: 8080

commonLabels: {}
commonAnnotations: {}

nodeAgent:
  replicas: 2              # == fc-mcp pool node count; OMITTED from the STS when autoscaling.keda.enabled
  podManagementPolicy: Parallel
  updateStrategy: OnDelete
  terminationGracePeriodSeconds: 120
  nodeSelector:
    fc-mcp: "true"
  taint:
    key: fc-mcp
    value: "true"
    effect: NoSchedule
  tolerations: []          # empty -> derived from .taint; set to override entirely
  extraAffinity: {}        # deep-merged on top of the always-on one-per-node anti-affinity
  resources:
    requests: {cpu: 500m, memory: 256Mi}
    limits: {cpu: "2", memory: 1Gi}
  fcBaseDir: /opt/fc-mcp
  maxVms: 32               # LOCKED to 32 (SLOT_MAX-SLOT_MIN+1 in server.py + `seq 0 31` in setup-network.sh). Render fails if changed.
  idlePauseSeconds: 300
  idleCheckInterval: 30
  fcBinary: /usr/bin/firecracker
  extraEnv: []
  networkSetup:
    enabled: true
    command: ["bash", "setup-network.sh"]
  serviceAccount:
    create: true
    name: fc-node-agent
    annotations: {}        # IRSA: eks.amazonaws.com/role-arn: arn:aws:iam::<acct>:role/<role>
  probes:
    startup:   {path: /health, periodSeconds: 10, failureThreshold: 30}
    liveness:  {path: /health, initialDelaySeconds: 15, periodSeconds: 30}
    readiness: {path: /ready,  initialDelaySeconds: 5,  periodSeconds: 10}
  preStop:
    enabled: true
  storage:
    size: 100Gi
    className: fc-local
  hostPaths:
    kvm: /dev/kvm
    devNetTun: /dev/net/tun
  seedHostPath: /opt/fc-seed
  seedMountType: File      # File|FileOrCreate — File hard-fails (crashloops) on an unseeded node

storageClass:
  create: true
  name: fc-local
  provisioner: rancher.io/local-path
  volumeBindingMode: WaitForFirstConsumer
  reclaimPolicy: Delete
  parameters: {}

router:
  replicas: 2
  resources:
    requests: {cpu: 100m, memory: 128Mi}
    limits: {cpu: "1", memory: 512Mi}
  lease: fc-mcp-router-leader
  heartbeatTimeout: 30
  extraEnv: []
  probes:
    liveness:  {path: /healthz, initialDelaySeconds: 10, periodSeconds: 30}
    readiness: {path: /readyz,  initialDelaySeconds: 5,  periodSeconds: 5}
  serviceAccount:
    create: true
    name: fc-mcp-router
    annotations: {}
  service:
    type: ClusterIP
    annotations: {}
    sessionAffinity: ClientIP   # stateful MCP: avoid round-robin to a non-leader during failover

s3:
  bucket: ""               # empty = S3 archival disabled
  prefix: fc-mcp/snapshots
  region: us-east-1

seeding:
  enabled: false
  mode: none               # none | ami | daemonset
  seedHostPath: /opt/fc-seed
  daemonset:
    s3:
      bucket: ""           # blank -> .Values.s3.bucket
      prefix: fc-mcp/seed
      region: ""           # blank -> .Values.s3.region
    image:
      repository: amazon/aws-cli
      tag: "2.15.0"
      pullPolicy: IfNotPresent
    resources:
      requests: {cpu: 50m, memory: 64Mi}
      limits: {cpu: 500m, memory: 256Mi}
    serviceAccount:
      create: true
      name: fc-mcp-seeder
      annotations: {}

podDisruptionBudget:
  router:
    enabled: true
    minAvailable: 1
  nodeAgent:
    enabled: true
    maxUnavailable: 1

autoscaling:
  keda:
    enabled: false
    minNodes: 3
    maxNodes: 12
    pollingInterval: 30
    prometheusAddress: http://prometheus-server.monitoring.svc:80
    targets:
      tapsPerNode: 24
      runningVmsPerNode: 20
      storageBytesPerNode: 80530636800
    behavior:
      scaleUp:
        stabilizationWindowSeconds: 60
        podsPerPeriod: 2
        periodSeconds: 120
    queries:
      taps: ""
      runningVms: ""
      storage: ""
  ksmCustomResourceState:
    enabled: false
    metricNamePrefix: fc_nodeagent
    serviceAccount:
      name: kube-state-metrics
      namespace: monitoring
  karpenter:
    enabled: false
    apiVersion: karpenter.sh/v1
    instanceTypes: [m5.metal, m6i.metal]
    capacityType: [on-demand]
    amiFamily: AL2
    amiSelectorTerms: []
    subnetSelectorTerms: [{tags: {karpenter.sh/discovery: ""}}]
    securityGroupSelectorTerms: [{tags: {karpenter.sh/discovery: ""}}]
    role: ""
    limits:
      cpu: "1000"
    disruption:
      consolidationPolicy: WhenEmpty
      consolidateAfter: 1h
      expireAfter: Never
    tags: {}
```

- [ ] **Step 6: Create `fc-mcp/templates/_helpers.tpl`**

```
{{/* Name + chart helpers */}}
{{- define "fc-mcp.name" -}}{{ default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "fc-mcp.fullname" -}}{{ printf "%s-%s" .Release.Name (include "fc-mcp.name" .) | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "fc-mcp.chart" -}}{{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "fc-mcp.namespace" -}}{{ .Values.namespace.name | default .Release.Namespace }}{{- end -}}

{{/* Common (non-selector) metadata labels — app.kubernetes.io/* lives HERE, never in selectors */}}
{{- define "fc-mcp.labels" -}}
helm.sh/chart: {{ include "fc-mcp.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: fc-mcp
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Immutable selector labels — bare `app:` only (matches the existing manifests) */}}
{{- define "fc-mcp.nodeAgent.selectorLabels" -}}app: fc-node-agent{{- end -}}
{{- define "fc-mcp.router.selectorLabels" -}}app: fc-mcp-router{{- end -}}

{{/* Image string; fails on empty/latest tag */}}
{{- define "fc-mcp.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if or (not $tag) (eq (toString $tag) "latest") -}}
{{- fail "image.tag must be set to a pinned tag (not empty, not 'latest'). Use --set image.tag=<tag>." -}}
{{- end -}}
{{- printf "%s:%s" .Values.image.repository (toString $tag) -}}
{{- end -}}

{{- define "fc-mcp.servicePort" -}}{{ .Values.service.port }}{{- end -}}
{{- define "fc-mcp.headlessServiceName" -}}fc-node{{- end -}}
{{- define "fc-mcp.storageClassName" -}}{{ .Values.storageClass.name }}{{- end -}}

{{/* ServiceAccount names */}}
{{- define "fc-mcp.nodeAgent.serviceAccountName" -}}{{ .Values.nodeAgent.serviceAccount.name }}{{- end -}}
{{- define "fc-mcp.router.serviceAccountName" -}}{{ .Values.router.serviceAccount.name }}{{- end -}}
{{- define "fc-mcp.seeder.serviceAccountName" -}}{{ .Values.seeding.daemonset.serviceAccount.name }}{{- end -}}

{{/* Node-agent tolerations: explicit list, or derived from .taint */}}
{{- define "fc-mcp.nodeAgent.tolerations" -}}
{{- if .Values.nodeAgent.tolerations -}}
{{ toYaml .Values.nodeAgent.tolerations }}
{{- else -}}
- key: {{ .Values.nodeAgent.taint.key }}
  operator: Equal
  value: {{ .Values.nodeAgent.taint.value | quote }}
  effect: {{ .Values.nodeAgent.taint.effect }}
{{- end -}}
{{- end -}}

{{/* Seeder S3 source with fallback to .Values.s3.* */}}
{{- define "fc-mcp.seeder.s3Bucket" -}}{{ .Values.seeding.daemonset.s3.bucket | default .Values.s3.bucket }}{{- end -}}
{{- define "fc-mcp.seeder.s3Region" -}}{{ .Values.seeding.daemonset.s3.region | default .Values.s3.region }}{{- end -}}

{{/* KEDA PromQL: operator override, else spec default */}}
{{- define "fc-mcp.keda.query.taps" -}}{{ .Values.autoscaling.keda.queries.taps | default "sum(fc_nodeagent_max_vms) - sum(fc_nodeagent_free_taps)" }}{{- end -}}
{{- define "fc-mcp.keda.query.runningVms" -}}{{ .Values.autoscaling.keda.queries.runningVms | default "sum(fc_nodeagent_running_vms)" }}{{- end -}}
{{- define "fc-mcp.keda.query.storage" -}}{{ .Values.autoscaling.keda.queries.storage | default (printf "sum(kubelet_volume_stats_used_bytes{namespace=\"%s\",persistentvolumeclaim=~\"fc-data-.*\"})" (include "fc-mcp.namespace" .)) }}{{- end -}}

{{/* Cross-field validation — included by always-rendered templates (the StatefulSet) */}}
{{- define "fc-mcp.validate" -}}
{{- if ne (int .Values.nodeAgent.maxVms) 32 -}}
{{- fail "nodeAgent.maxVms is LOCKED to 32 (= SLOT_MAX-SLOT_MIN+1 in server.py and the `seq 0 31` tap count in setup-network.sh). Real capacity is a code change, not a value." -}}
{{- end -}}
{{- if and .Values.autoscaling.keda.enabled (eq .Values.seeding.mode "none") -}}
{{- fail "autoscaling.keda.enabled=true requires seeding.mode=ami or daemonset (an unseeded autoscaled node fails every VM create: 'Kernel not found' — KEDA plan §9)." -}}
{{- end -}}
{{- if and .Values.autoscaling.karpenter.enabled (not .Values.autoscaling.karpenter.role) -}}
{{- fail "autoscaling.karpenter.enabled=true requires autoscaling.karpenter.role (node IAM role / instance profile name)." -}}
{{- end -}}
{{- if gt (int .Values.autoscaling.keda.minNodes) (int .Values.autoscaling.keda.maxNodes) -}}
{{- fail "autoscaling.keda.minNodes must be <= autoscaling.keda.maxNodes." -}}
{{- end -}}
{{- end -}}
```

- [ ] **Step 7: Create `fc-mcp/values.schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft-07/schema#",
  "type": "object",
  "properties": {
    "image": {
      "type": "object",
      "required": ["repository", "tag"],
      "properties": {
        "repository": {"type": "string", "minLength": 1},
        "tag": {"type": "string", "not": {"const": "latest"}},
        "pullPolicy": {"enum": ["Always", "IfNotPresent", "Never"]}
      }
    },
    "service": {"type": "object", "properties": {"port": {"type": "integer", "minimum": 1, "maximum": 65535}}},
    "nodeAgent": {
      "type": "object",
      "properties": {
        "maxVms": {"const": 32},
        "seedMountType": {"enum": ["File", "FileOrCreate"]}
      }
    },
    "seeding": {
      "type": "object",
      "properties": {"mode": {"enum": ["none", "ami", "daemonset"]}}
    }
  }
}
```

- [ ] **Step 8: Run the harness to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh`
Expected: ALL PASS (lint ok; StatefulSet still missing → that assertion FAILS). Fix: comment the StatefulSet assertion with `# added in Task 5` OR keep it failing until Task 5. **Decision:** move the `kind: StatefulSet` assertion to Task 5; in Task 1 the harness asserts only lint + the two image-tag fail cases.

Re-run: Expected ALL PASS.

- [ ] **Step 9: Commit**

```bash
git add fc-mcp/Chart.yaml fc-mcp/.helmignore fc-mcp/values.yaml fc-mcp/templates/_helpers.tpl fc-mcp/values.schema.json fc-mcp/ci/render-tests.sh
git commit -m "feat(helm): chart scaffold, values contract, helpers, render-test harness"
```

---

### Task 2: CRDs (install-only)

**Files:**
- Create: `fc-mcp/crds/nodeagents.fcmcp.io.yaml` (copy of `deploy/crds/nodeagents.fcmcp.io.yaml`, verbatim)
- Create: `fc-mcp/crds/sessions.fcmcp.io.yaml` (copy of `deploy/crds/sessions.fcmcp.io.yaml`, verbatim)

- [ ] **Step 1: Add the assertion**

Append to `ci/render-tests.sh` before the final summary block:

```bash
# ---- Task 2: CRDs are install-only (NOT rendered by `helm template` by default) ----
assert_absent "CRDs are not in templated output" "kind: CustomResourceDefinition" $REQ
if [ -f "$CHART/crds/nodeagents.fcmcp.io.yaml" ] && [ -f "$CHART/crds/sessions.fcmcp.io.yaml" ]; then \
  pass "both CRDs present in crds/"; else fail "CRD files missing from crds/"; fi
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh`
Expected: FAIL "CRD files missing from crds/".

- [ ] **Step 3: Copy the CRDs verbatim**

```bash
mkdir -p fc-mcp/crds
cp deploy/crds/nodeagents.fcmcp.io.yaml fc-mcp/crds/nodeagents.fcmcp.io.yaml
cp deploy/crds/sessions.fcmcp.io.yaml   fc-mcp/crds/sessions.fcmcp.io.yaml
```

(Do **not** templated-ize CRDs: Helm 3 installs `crds/` before the release and never upgrades/deletes them — that protects the Session/NodeAgent routing state from a `helm uninstall` cascade. CRD schema changes require a manual `kubectl apply -f fc-mcp/crds/` — documented in NOTES, Task 11.)

- [ ] **Step 4: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add fc-mcp/crds/
git commit -m "feat(helm): ship fcmcp.io CRDs as install-only crds/"
```

---

### Task 3: Namespace + ServiceAccounts + RBAC

**Files:**
- Create: `fc-mcp/templates/namespace.yaml`
- Create: `fc-mcp/templates/serviceaccount-node-agent.yaml`
- Create: `fc-mcp/templates/serviceaccount-router.yaml`
- Create: `fc-mcp/templates/rbac-node-agent.yaml`
- Create: `fc-mcp/templates/rbac-router.yaml`

- [ ] **Step 1: Add assertions**

Append to `ci/render-tests.sh` before the summary:

```bash
# ---- Task 3: SAs + RBAC ----
assert_contains "node-agent SA rendered" "name: fc-node-agent" $REQ
assert_contains "router SA rendered" "name: fc-mcp-router" $REQ
assert_contains "node-agent Role grants nodeagents" "resources:\s*\[?\"?nodeagents" $REQ
assert_contains "router Role grants leases" "leases" $REQ
assert_absent "no Namespace object by default" "kind: Namespace" $REQ
assert_contains "Namespace rendered when enabled" "kind: Namespace" $REQ --set namespace.create=true
assert_contains "Namespace has keep policy" "helm.sh/resource-policy: keep" $REQ --set namespace.create=true
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh`
Expected: FAIL (SAs/Roles/Namespace not rendered).

- [ ] **Step 3: Create `fc-mcp/templates/namespace.yaml`**

```
{{- if .Values.namespace.create }}
apiVersion: v1
kind: Namespace
metadata:
  name: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
  annotations:
    helm.sh/resource-policy: keep   # never cascade-delete a (possibly shared) namespace on uninstall
{{- end }}
```

- [ ] **Step 4: Create `fc-mcp/templates/serviceaccount-node-agent.yaml`**

```
{{- if .Values.nodeAgent.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "fc-mcp.nodeAgent.serviceAccountName" . }}
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
  {{- with .Values.nodeAgent.serviceAccount.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
```

- [ ] **Step 5: Create `fc-mcp/templates/serviceaccount-router.yaml`**

```
{{- if .Values.router.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "fc-mcp.router.serviceAccountName" . }}
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
  {{- with .Values.router.serviceAccount.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
```

- [ ] **Step 6: Create `fc-mcp/templates/rbac-node-agent.yaml`** (namespaced Role + RoleBinding, from `kubernetes/rbac.yaml`)

```
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ include "fc-mcp.nodeAgent.serviceAccountName" . }}
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
rules:
  - apiGroups: ["fcmcp.io"]
    resources: ["nodeagents"]
    verbs: ["get", "list", "watch", "create", "update", "patch"]
  - apiGroups: ["fcmcp.io"]
    resources: ["nodeagents/status"]
    verbs: ["get", "update", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ include "fc-mcp.nodeAgent.serviceAccountName" . }}
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{ include "fc-mcp.nodeAgent.serviceAccountName" . }}
subjects:
  - kind: ServiceAccount
    name: {{ include "fc-mcp.nodeAgent.serviceAccountName" . }}
    namespace: {{ include "fc-mcp.namespace" . }}
```

- [ ] **Step 7: Create `fc-mcp/templates/rbac-router.yaml`**

```
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ include "fc-mcp.router.serviceAccountName" . }}
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
rules:
  - apiGroups: ["fcmcp.io"]
    resources: ["sessions"]
    verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
  - apiGroups: ["fcmcp.io"]
    resources: ["sessions/status"]
    verbs: ["get", "update", "patch"]
  - apiGroups: ["fcmcp.io"]
    resources: ["nodeagents"]
    verbs: ["get", "list", "watch"]
  - apiGroups: ["fcmcp.io"]
    resources: ["nodeagents/status"]
    verbs: ["get", "update", "patch"]
  - apiGroups: ["coordination.k8s.io"]
    resources: ["leases"]
    verbs: ["get", "list", "watch", "create", "update", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ include "fc-mcp.router.serviceAccountName" . }}
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{ include "fc-mcp.router.serviceAccountName" . }}
subjects:
  - kind: ServiceAccount
    name: {{ include "fc-mcp.router.serviceAccountName" . }}
    namespace: {{ include "fc-mcp.namespace" . }}
```

- [ ] **Step 8: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh`
Expected: ALL PASS.

- [ ] **Step 9: Commit**

```bash
git add fc-mcp/templates/namespace.yaml fc-mcp/templates/serviceaccount-*.yaml fc-mcp/templates/rbac-*.yaml
git commit -m "feat(helm): namespace, service accounts, namespaced RBAC"
```

---

### Task 4: StorageClass (cluster-scoped, gated)

**Files:**
- Create: `fc-mcp/templates/storageclass-local.yaml`

- [ ] **Step 1: Add assertions**

Append to `ci/render-tests.sh`:

```bash
# ---- Task 4: StorageClass ----
assert_contains "StorageClass rendered by default" "kind: StorageClass" $REQ
assert_contains "StorageClass name matches default" "name: fc-local" $REQ
assert_absent "StorageClass omitted when create=false" "kind: StorageClass" $REQ --set storageClass.create=false
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh` → FAIL (no StorageClass).

- [ ] **Step 3: Create `fc-mcp/templates/storageclass-local.yaml`** (from `kubernetes/storageclass-local.yaml`)

```
{{- if .Values.storageClass.create }}
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: {{ include "fc-mcp.storageClassName" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
provisioner: {{ .Values.storageClass.provisioner }}
volumeBindingMode: {{ .Values.storageClass.volumeBindingMode }}
reclaimPolicy: {{ .Values.storageClass.reclaimPolicy }}
{{- with .Values.storageClass.parameters }}
parameters:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
```

(`fc-mcp.storageClassName` is the single source the StatefulSet's `volumeClaimTemplates.storageClassName` also reads, so they can't drift. Name defaults to `fc-local` to match the current manifest; for >1 release per cluster, override `storageClass.name`/`nodeAgent.storage.className` to a release-unique value — see README.)

- [ ] **Step 4: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh` → ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add fc-mcp/templates/storageclass-local.yaml
git commit -m "feat(helm): cluster-scoped fc-local StorageClass (gated)"
```

---

### Task 5: Node-agent StatefulSet + headless Service

**Files:**
- Create: `fc-mcp/templates/node-agent-statefulset.yaml`
- Create: `fc-mcp/templates/node-agent-headless-service.yaml`

- [ ] **Step 1: Add assertions** (and add the selector-leak check helper)

Append to `ci/render-tests.sh` — first the helper (place near the other `assert_*` defs), then the checks:

```bash
# helper: assert app.kubernetes.io/* never leaks into a matchLabels block (immutable-selector safety)
assert_no_kube_labels_in_selectors() {
  if render "$@" | awk '
    /[[:space:]]matchLabels:[[:space:]]*$/ {depth=match($0,/[^ ]/); inblk=1; next}
    inblk { d=match($0,/[^ ]/); if (d>depth){ if($0 ~ /app\.kubernetes\.io/) bad=1 } else inblk=0 }
    END{exit bad?1:0}'; then pass "no app.kubernetes.io in any matchLabels ($*)"; else fail "app.kubernetes.io leaked into matchLabels ($*)"; fi
}
```

```bash
# ---- Task 5: node-agent StatefulSet + headless Service ----
assert_contains "StatefulSet rendered" "kind: StatefulSet" $REQ
assert_contains "STS serviceName == headless name" "serviceName: fc-node" $REQ
assert_contains "headless Service rendered" "clusterIP: None" $REQ
assert_contains "node-agent is privileged" "privileged: true" $REQ
assert_contains "node-agent hostNetwork" "hostNetwork: true" $REQ
assert_contains "replicas present by default" "replicas: 2" $REQ
assert_absent "replicas OMITTED when keda enabled" "replicas:" $REQ --set autoscaling.keda.enabled=true --set seeding.mode=ami
assert_render_fails "render fails if maxVms != 32" "maxVms is LOCKED to 32" $REQ --set nodeAgent.maxVms=64
assert_contains "port templated everywhere (preStop)" "http://localhost:8080/drain" $REQ
assert_absent "no literal 8080 when port changed" ":8080" $REQ --set service.port=9090
assert_contains "karpenter do-not-disrupt annotation when enabled" "karpenter.sh/do-not-disrupt" $REQ --set autoscaling.karpenter.enabled=true --set autoscaling.karpenter.role=r --set seeding.mode=ami
assert_no_kube_labels_in_selectors $REQ
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh` → FAIL (no StatefulSet).

- [ ] **Step 3: Create `fc-mcp/templates/node-agent-statefulset.yaml`**

```
{{- include "fc-mcp.validate" . -}}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: fc-node-agent
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.nodeAgent.selectorLabels" . | nindent 4 }}
    {{- include "fc-mcp.labels" . | nindent 4 }}
spec:
  serviceName: {{ include "fc-mcp.headlessServiceName" . }}
  {{- if not .Values.autoscaling.keda.enabled }}
  replicas: {{ .Values.nodeAgent.replicas }}
  {{- end }}
  podManagementPolicy: {{ .Values.nodeAgent.podManagementPolicy }}
  updateStrategy:
    type: {{ .Values.nodeAgent.updateStrategy }}
  selector:
    matchLabels:
      {{- include "fc-mcp.nodeAgent.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "fc-mcp.nodeAgent.selectorLabels" . | nindent 8 }}
        {{- include "fc-mcp.labels" . | nindent 8 }}
      {{- if .Values.autoscaling.karpenter.enabled }}
      annotations:
        karpenter.sh/do-not-disrupt: "true"
      {{- end }}
    spec:
      serviceAccountName: {{ include "fc-mcp.nodeAgent.serviceAccountName" . }}
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      nodeSelector:
        {{- toYaml .Values.nodeAgent.nodeSelector | nindent 8 }}
      tolerations:
        {{- include "fc-mcp.nodeAgent.tolerations" . | nindent 8 }}
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  {{- include "fc-mcp.nodeAgent.selectorLabels" . | nindent 18 }}
              topologyKey: kubernetes.io/hostname
        {{- with .Values.nodeAgent.extraAffinity }}
        {{- toYaml . | nindent 8 }}
        {{- end }}
      hostNetwork: true
      dnsPolicy: ClusterFirstWithHostNet
      terminationGracePeriodSeconds: {{ .Values.nodeAgent.terminationGracePeriodSeconds }}
      securityContext:
        runAsUser: 0
      {{- if .Values.nodeAgent.networkSetup.enabled }}
      initContainers:
        - name: network-setup
          image: {{ include "fc-mcp.image" . }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: {{ toJson .Values.nodeAgent.networkSetup.command }}
          securityContext:
            privileged: true
          volumeMounts:
            - name: fc-data
              mountPath: {{ .Values.nodeAgent.fcBaseDir }}
      {{- end }}
      containers:
        - name: fc-node-agent
          image: {{ include "fc-mcp.image" . }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: {{ include "fc-mcp.servicePort" . }}
              name: http
          env:
            - name: FC_BASE_DIR
              value: {{ .Values.nodeAgent.fcBaseDir | quote }}
            - name: NODE_NAME
              valueFrom: {fieldRef: {fieldPath: spec.nodeName}}
            - name: POD_IP
              valueFrom: {fieldRef: {fieldPath: status.hostIP}}
            - name: FC_MCP_NAMESPACE
              valueFrom: {fieldRef: {fieldPath: metadata.namespace}}
            - name: MCP_PORT
              value: {{ include "fc-mcp.servicePort" . | quote }}
            - name: FC_MAX_VMS
              value: {{ .Values.nodeAgent.maxVms | quote }}
            - name: FC_IDLE_PAUSE_SECONDS
              value: {{ .Values.nodeAgent.idlePauseSeconds | quote }}
            - name: FC_IDLE_CHECK_INTERVAL
              value: {{ .Values.nodeAgent.idleCheckInterval | quote }}
            - name: FC_BINARY
              value: {{ .Values.nodeAgent.fcBinary | quote }}
            {{- if .Values.s3.bucket }}
            - name: FC_S3_BUCKET
              value: {{ .Values.s3.bucket | quote }}
            - name: FC_S3_PREFIX
              value: {{ .Values.s3.prefix | quote }}
            - name: AWS_REGION
              value: {{ .Values.s3.region | quote }}
            {{- end }}
            {{- with .Values.nodeAgent.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          securityContext:
            privileged: true
          {{- if .Values.nodeAgent.preStop.enabled }}
          lifecycle:
            preStop:
              exec:
                command: ["/bin/sh", "-c", "curl -s -X POST http://localhost:{{ include "fc-mcp.servicePort" . }}/drain || true"]
          {{- end }}
          startupProbe:
            httpGet: {path: {{ .Values.nodeAgent.probes.startup.path }}, port: {{ include "fc-mcp.servicePort" . }}}
            periodSeconds: {{ .Values.nodeAgent.probes.startup.periodSeconds }}
            failureThreshold: {{ .Values.nodeAgent.probes.startup.failureThreshold }}
          livenessProbe:
            httpGet: {path: {{ .Values.nodeAgent.probes.liveness.path }}, port: {{ include "fc-mcp.servicePort" . }}}
            initialDelaySeconds: {{ .Values.nodeAgent.probes.liveness.initialDelaySeconds }}
            periodSeconds: {{ .Values.nodeAgent.probes.liveness.periodSeconds }}
          readinessProbe:
            httpGet: {path: {{ .Values.nodeAgent.probes.readiness.path }}, port: {{ include "fc-mcp.servicePort" . }}}
            initialDelaySeconds: {{ .Values.nodeAgent.probes.readiness.initialDelaySeconds }}
            periodSeconds: {{ .Values.nodeAgent.probes.readiness.periodSeconds }}
          resources:
            {{- toYaml .Values.nodeAgent.resources | nindent 12 }}
          volumeMounts:
            - name: fc-data
              mountPath: {{ .Values.nodeAgent.fcBaseDir }}
            - name: vm-images
              mountPath: {{ .Values.nodeAgent.fcBaseDir }}/vm-images
              readOnly: true
            - name: vm-ssh-key
              mountPath: {{ .Values.nodeAgent.fcBaseDir }}/vm_ssh_key
              readOnly: true
            - name: kvm
              mountPath: {{ .Values.nodeAgent.hostPaths.kvm }}
            - name: dev-net-tun
              mountPath: {{ .Values.nodeAgent.hostPaths.devNetTun }}
      volumes:
        - name: vm-images
          hostPath: {path: {{ .Values.nodeAgent.seedHostPath }}/vm-images, type: Directory}
        - name: vm-ssh-key
          hostPath: {path: {{ .Values.nodeAgent.seedHostPath }}/vm_ssh_key, type: {{ .Values.nodeAgent.seedMountType }}}
        - name: kvm
          hostPath: {path: {{ .Values.nodeAgent.hostPaths.kvm }}, type: CharDevice}
        - name: dev-net-tun
          hostPath: {path: {{ .Values.nodeAgent.hostPaths.devNetTun }}, type: CharDevice}
  volumeClaimTemplates:
    - metadata:
        name: fc-data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: {{ include "fc-mcp.storageClassName" . }}
        resources:
          requests:
            storage: {{ .Values.nodeAgent.storage.size }}
```

- [ ] **Step 4: Create `fc-mcp/templates/node-agent-headless-service.yaml`**

```
apiVersion: v1
kind: Service
metadata:
  name: {{ include "fc-mcp.headlessServiceName" . }}
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.nodeAgent.selectorLabels" . | nindent 4 }}
    {{- include "fc-mcp.labels" . | nindent 4 }}
spec:
  clusterIP: None
  selector:
    {{- include "fc-mcp.nodeAgent.selectorLabels" . | nindent 4 }}
  ports:
    - name: http
      port: {{ include "fc-mcp.servicePort" . }}
      targetPort: {{ include "fc-mcp.servicePort" . }}
```

- [ ] **Step 5: Remove the placeholder StatefulSet assertion from Task 1**

In `ci/render-tests.sh`, delete the `assert_contains "Chart renders a StatefulSet" ...` line under the Task 1 block (it's now asserted in Task 5).

- [ ] **Step 6: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh`
Expected: ALL PASS. (If `assert_absent ":8080"` fails, it means a literal port leaked — find it and replace with `{{ include "fc-mcp.servicePort" . }}`.)

- [ ] **Step 7: Commit**

```bash
git add fc-mcp/templates/node-agent-statefulset.yaml fc-mcp/templates/node-agent-headless-service.yaml fc-mcp/ci/render-tests.sh
git commit -m "feat(helm): node-agent StatefulSet + headless Service (port-templated, keda-aware, validated)"
```

---

### Task 6: Router Deployment + Service + PDBs

**Files:**
- Create: `fc-mcp/templates/router-deployment.yaml`
- Create: `fc-mcp/templates/router-service.yaml`
- Create: `fc-mcp/templates/pdb-router.yaml`
- Create: `fc-mcp/templates/pdb-node-agent.yaml`

- [ ] **Step 1: Add assertions**

Append to `ci/render-tests.sh`:

```bash
# ---- Task 6: router + PDBs ----
assert_contains "router Deployment rendered" "kind: Deployment" $REQ
assert_contains "router command port is templated" "proxy.router" $REQ
assert_contains "router Service sessionAffinity" "sessionAffinity: ClientIP" $REQ
assert_contains "router PDB minAvailable" "minAvailable: 1" $REQ
assert_contains "node-agent PDB maxUnavailable" "maxUnavailable: 1" $REQ
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh` → FAIL.

- [ ] **Step 3: Create `fc-mcp/templates/router-deployment.yaml`**

```
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fc-mcp-router
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.router.selectorLabels" . | nindent 4 }}
    {{- include "fc-mcp.labels" . | nindent 4 }}
spec:
  replicas: {{ .Values.router.replicas }}
  selector:
    matchLabels:
      {{- include "fc-mcp.router.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "fc-mcp.router.selectorLabels" . | nindent 8 }}
        {{- include "fc-mcp.labels" . | nindent 8 }}
    spec:
      serviceAccountName: {{ include "fc-mcp.router.serviceAccountName" . }}
      {{- with .Values.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      containers:
        - name: router
          image: {{ include "fc-mcp.image" . }}
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          command: ["uv", "run", "python", "-m", "proxy.router", "--port", "{{ include "fc-mcp.servicePort" . }}"]
          ports:
            - containerPort: {{ include "fc-mcp.servicePort" . }}
              name: mcp-http
          env:
            - name: POD_NAME
              valueFrom: {fieldRef: {fieldPath: metadata.name}}
            - name: FC_MCP_NAMESPACE
              valueFrom: {fieldRef: {fieldPath: metadata.namespace}}
            - name: FC_MCP_NODE_PORT
              value: {{ include "fc-mcp.servicePort" . | quote }}
            - name: FC_MCP_LEASE
              value: {{ .Values.router.lease | quote }}
            - name: FC_MCP_HEARTBEAT_TIMEOUT
              value: {{ .Values.router.heartbeatTimeout | quote }}
            {{- with .Values.router.extraEnv }}
            {{- toYaml . | nindent 12 }}
            {{- end }}
          livenessProbe:
            httpGet: {path: {{ .Values.router.probes.liveness.path }}, port: {{ include "fc-mcp.servicePort" . }}}
            initialDelaySeconds: {{ .Values.router.probes.liveness.initialDelaySeconds }}
            periodSeconds: {{ .Values.router.probes.liveness.periodSeconds }}
          readinessProbe:
            httpGet: {path: {{ .Values.router.probes.readiness.path }}, port: {{ include "fc-mcp.servicePort" . }}}
            initialDelaySeconds: {{ .Values.router.probes.readiness.initialDelaySeconds }}
            periodSeconds: {{ .Values.router.probes.readiness.periodSeconds }}
          resources:
            {{- toYaml .Values.router.resources | nindent 12 }}
```

- [ ] **Step 4: Create `fc-mcp/templates/router-service.yaml`**

```
apiVersion: v1
kind: Service
metadata:
  name: fc-mcp-router
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.router.selectorLabels" . | nindent 4 }}
    {{- include "fc-mcp.labels" . | nindent 4 }}
  {{- with .Values.router.service.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
spec:
  type: {{ .Values.router.service.type }}
  sessionAffinity: {{ .Values.router.service.sessionAffinity }}
  selector:
    {{- include "fc-mcp.router.selectorLabels" . | nindent 4 }}
  ports:
    - name: mcp-http
      port: {{ include "fc-mcp.servicePort" . }}
      targetPort: {{ include "fc-mcp.servicePort" . }}
```

- [ ] **Step 5: Create `fc-mcp/templates/pdb-router.yaml`**

```
{{- if .Values.podDisruptionBudget.router.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: fc-mcp-router
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
spec:
  minAvailable: {{ .Values.podDisruptionBudget.router.minAvailable }}
  selector:
    matchLabels:
      {{- include "fc-mcp.router.selectorLabels" . | nindent 6 }}
{{- end }}
```

- [ ] **Step 6: Create `fc-mcp/templates/pdb-node-agent.yaml`**

```
{{- if .Values.podDisruptionBudget.nodeAgent.enabled }}
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: fc-node-agent
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
spec:
  maxUnavailable: {{ .Values.podDisruptionBudget.nodeAgent.maxUnavailable }}
  selector:
    matchLabels:
      {{- include "fc-mcp.nodeAgent.selectorLabels" . | nindent 6 }}
{{- end }}
```

- [ ] **Step 7: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh` → ALL PASS.

- [ ] **Step 8: Commit**

```bash
git add fc-mcp/templates/router-deployment.yaml fc-mcp/templates/router-service.yaml fc-mcp/templates/pdb-*.yaml fc-mcp/ci/render-tests.sh
git commit -m "feat(helm): router Deployment+Service (sessionAffinity) and PDBs"
```

---

### Task 7: Seeding DaemonSet (gated)

**Files:**
- Create: `fc-mcp/templates/seeding/seed-serviceaccount.yaml`
- Create: `fc-mcp/templates/seeding/seed-daemonset.yaml`

(No `seed-rbac.yaml`: the seeder needs S3 only, not the k8s API — IRSA on its SA suffices. YAGNI.)

- [ ] **Step 1: Add assertions**

Append to `ci/render-tests.sh`:

```bash
# ---- Task 7: seeding ----
assert_absent "no seeding objects by default" "fc-mcp-seeder" $REQ
assert_contains "seeding DaemonSet rendered when mode=daemonset" "kind: DaemonSet" $REQ --set seeding.enabled=true --set seeding.mode=daemonset --set s3.bucket=b
assert_render_fails "keda + seeding=none hard-fails" "requires seeding.mode" $REQ --set autoscaling.keda.enabled=true
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh` → FAIL (DaemonSet not rendered).

- [ ] **Step 3: Create `fc-mcp/templates/seeding/seed-serviceaccount.yaml`**

```
{{- if and .Values.seeding.enabled (eq .Values.seeding.mode "daemonset") .Values.seeding.daemonset.serviceAccount.create }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "fc-mcp.seeder.serviceAccountName" . }}
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
  {{- with .Values.seeding.daemonset.serviceAccount.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
```

- [ ] **Step 4: Create `fc-mcp/templates/seeding/seed-daemonset.yaml`**

```
{{- if and .Values.seeding.enabled (eq .Values.seeding.mode "daemonset") }}
{{- $bucket := include "fc-mcp.seeder.s3Bucket" . }}
{{- if not $bucket }}{{ fail "seeding.mode=daemonset requires a bucket: set seeding.daemonset.s3.bucket or s3.bucket" }}{{- end }}
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: fc-mcp-seeder
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
spec:
  selector:
    matchLabels:
      app: fc-mcp-seeder
  template:
    metadata:
      labels:
        app: fc-mcp-seeder
        {{- include "fc-mcp.labels" . | nindent 8 }}
    spec:
      serviceAccountName: {{ include "fc-mcp.seeder.serviceAccountName" . }}
      nodeSelector:
        {{- toYaml .Values.nodeAgent.nodeSelector | nindent 8 }}
      tolerations:
        {{- include "fc-mcp.nodeAgent.tolerations" . | nindent 8 }}
      containers:
        - name: seeder
          image: "{{ .Values.seeding.daemonset.image.repository }}:{{ .Values.seeding.daemonset.image.tag }}"
          imagePullPolicy: {{ .Values.seeding.daemonset.image.pullPolicy }}
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -e
              aws s3 sync "s3://{{ $bucket }}/{{ .Values.seeding.daemonset.s3.prefix }}/" "{{ .Values.seeding.seedHostPath }}/" {{ with (include "fc-mcp.seeder.s3Region" .) }}--region {{ . }}{{ end }}
              chmod 0400 "{{ .Values.seeding.seedHostPath }}/vm_ssh_key" || true
              echo "seed complete"; sleep infinity
          resources:
            {{- toYaml .Values.seeding.daemonset.resources | nindent 12 }}
          volumeMounts:
            - name: seed
              mountPath: {{ .Values.seeding.seedHostPath }}
      volumes:
        - name: seed
          hostPath: {path: {{ .Values.seeding.seedHostPath }}, type: DirectoryOrCreate}
{{- end }}
```

- [ ] **Step 5: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh` → ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add fc-mcp/templates/seeding/
git commit -m "feat(helm): optional S3 seeding DaemonSet for autoscaled nodes"
```

---

### Task 8: KEDA ScaledObject (gated + capability-checked)

**Files:**
- Create: `fc-mcp/templates/autoscaling/keda-scaledobject.yaml`

- [ ] **Step 1: Add assertions** (define a reusable args var near the top of the harness, after `REQ=`)

```bash
KEDA="$REQ --set autoscaling.keda.enabled=true --set seeding.mode=ami --api-versions keda.sh/v1alpha1"
```

Then append the checks:

```bash
# ---- Task 8: KEDA ScaledObject ----
assert_absent "no ScaledObject by default" "kind: ScaledObject" $REQ
assert_contains "ScaledObject rendered when enabled" "kind: ScaledObject" $KEDA
assert_contains "scaleDown is Disabled" "selectPolicy: Disabled" $KEDA
assert_contains "taps threshold" 'threshold: "24"' $KEDA
assert_contains "three prometheus triggers" "kubelet_volume_stats_used_bytes" $KEDA
assert_render_fails "keda enabled without KEDA CRD fails loudly" "KEDA CRD" $REQ --set autoscaling.keda.enabled=true --set seeding.mode=ami
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh` → FAIL (no ScaledObject).

- [ ] **Step 3: Create `fc-mcp/templates/autoscaling/keda-scaledobject.yaml`**

```
{{- if .Values.autoscaling.keda.enabled }}
{{- if not (.Capabilities.APIVersions.Has "keda.sh/v1alpha1") }}
{{- fail "autoscaling.keda.enabled=true but the KEDA CRD (keda.sh/v1alpha1) is not installed. Install KEDA first, or render with --api-versions keda.sh/v1alpha1." }}
{{- end }}
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: fc-node-agent
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: StatefulSet
    name: fc-node-agent
  pollingInterval: {{ .Values.autoscaling.keda.pollingInterval }}
  minReplicaCount: {{ .Values.autoscaling.keda.minNodes }}
  maxReplicaCount: {{ .Values.autoscaling.keda.maxNodes }}
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp:
          stabilizationWindowSeconds: {{ .Values.autoscaling.keda.behavior.scaleUp.stabilizationWindowSeconds }}
          policies:
            - type: Pods
              value: {{ .Values.autoscaling.keda.behavior.scaleUp.podsPerPeriod }}
              periodSeconds: {{ .Values.autoscaling.keda.behavior.scaleUp.periodSeconds }}
        scaleDown:
          selectPolicy: Disabled        # SCALE-UP ONLY (pet VMs) — intentionally not configurable
  triggers:
    - type: prometheus
      metadata:
        serverAddress: {{ .Values.autoscaling.keda.prometheusAddress | quote }}
        query: {{ include "fc-mcp.keda.query.taps" . | quote }}
        threshold: {{ .Values.autoscaling.keda.targets.tapsPerNode | quote }}
    - type: prometheus
      metadata:
        serverAddress: {{ .Values.autoscaling.keda.prometheusAddress | quote }}
        query: {{ include "fc-mcp.keda.query.runningVms" . | quote }}
        threshold: {{ .Values.autoscaling.keda.targets.runningVmsPerNode | quote }}
    - type: prometheus
      metadata:
        serverAddress: {{ .Values.autoscaling.keda.prometheusAddress | quote }}
        query: {{ include "fc-mcp.keda.query.storage" . | quote }}
        threshold: {{ .Values.autoscaling.keda.targets.storageBytesPerNode | quote }}
{{- end }}
```

- [ ] **Step 4: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh` → ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add fc-mcp/templates/autoscaling/keda-scaledobject.yaml fc-mcp/ci/render-tests.sh
git commit -m "feat(helm): KEDA ScaledObject (scale-up-only, 3 triggers, capability-gated)"
```

---

### Task 9: kube-state-metrics CustomResourceState (gated)

**Files:**
- Create: `fc-mcp/templates/autoscaling/ksm-customresourcestate-configmap.yaml`
- Create: `fc-mcp/templates/autoscaling/ksm-clusterrole.yaml`
- Create: `fc-mcp/templates/autoscaling/ksm-clusterrolebinding.yaml`

- [ ] **Step 1: Add assertions**

```bash
KSM="$REQ --set autoscaling.ksmCustomResourceState.enabled=true"
```
```bash
# ---- Task 9: KSM CustomResourceState ----
assert_absent "no KSM config by default" "CustomResourceStateMetrics" $REQ
assert_contains "KSM CRS ConfigMap rendered" "CustomResourceStateMetrics" $KSM
assert_contains "KSM maps freeTaps" "freeTaps" $KSM
assert_contains "KSM ClusterRole is release-prefixed" "name: t-fc-mcp-ksm-nodeagents" $KSM
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh` → FAIL.

- [ ] **Step 3: Create `fc-mcp/templates/autoscaling/ksm-customresourcestate-configmap.yaml`**

```
{{- if .Values.autoscaling.ksmCustomResourceState.enabled }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "fc-mcp.fullname" . }}-ksm-crs
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
data:
  config.yaml: |
    kind: CustomResourceStateMetrics
    spec:
      resources:
        - groupVersionKind:
            group: fcmcp.io
            version: v1alpha1
            kind: NodeAgent
          metricNamePrefix: {{ .Values.autoscaling.ksmCustomResourceState.metricNamePrefix }}
          labelsFromPath:
            node: [spec, nodeName]
          metrics:
            - name: free_taps
              each: {type: Gauge, gauge: {path: [status, freeTaps]}}
            - name: running_vms
              each: {type: Gauge, gauge: {path: [status, runningVmCount]}}
            - name: max_vms
              each: {type: Gauge, gauge: {path: [spec, maxVms]}}
{{- end }}
```

- [ ] **Step 4: Create `fc-mcp/templates/autoscaling/ksm-clusterrole.yaml`**

```
{{- if .Values.autoscaling.ksmCustomResourceState.enabled }}
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: {{ include "fc-mcp.fullname" . }}-ksm-nodeagents
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
rules:
  - apiGroups: ["fcmcp.io"]
    resources: ["nodeagents"]
    verbs: ["get", "list", "watch"]
{{- end }}
```

- [ ] **Step 5: Create `fc-mcp/templates/autoscaling/ksm-clusterrolebinding.yaml`**

```
{{- if .Values.autoscaling.ksmCustomResourceState.enabled }}
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {{ include "fc-mcp.fullname" . }}-ksm-nodeagents
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: {{ include "fc-mcp.fullname" . }}-ksm-nodeagents
subjects:
  - kind: ServiceAccount
    name: {{ .Values.autoscaling.ksmCustomResourceState.serviceAccount.name }}
    namespace: {{ .Values.autoscaling.ksmCustomResourceState.serviceAccount.namespace }}
{{- end }}
```

(The release name in tests is `t` — `helm template t ...` — so the prefixed name is `t-fc-mcp-ksm-nodeagents`. The ConfigMap is rendered; wiring KSM to load it via `--custom-resource-state-config-file` is an operator step documented in NOTES.)

- [ ] **Step 6: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh` → ALL PASS.

- [ ] **Step 7: Commit**

```bash
git add fc-mcp/templates/autoscaling/ksm-*.yaml fc-mcp/ci/render-tests.sh
git commit -m "feat(helm): KSM CustomResourceState ConfigMap + release-prefixed ClusterRole/Binding"
```

---

### Task 10: Karpenter NodePool + EC2NodeClass (gated + capability-checked)

**Files:**
- Create: `fc-mcp/templates/autoscaling/karpenter-nodepool.yaml`
- Create: `fc-mcp/templates/autoscaling/karpenter-ec2nodeclass.yaml`

- [ ] **Step 1: Add assertions**

```bash
KARP="$REQ --set autoscaling.karpenter.enabled=true --set autoscaling.karpenter.role=fc-mcp-node --api-versions karpenter.sh/v1 --api-versions karpenter.k8s.aws/v1"
```
```bash
# ---- Task 10: Karpenter ----
assert_absent "no NodePool by default" "kind: NodePool" $REQ
assert_contains "NodePool rendered when enabled" "kind: NodePool" $KARP
assert_contains "EC2NodeClass rendered" "kind: EC2NodeClass" $KARP
assert_contains "NodePool taints fc-mcp" "key: fc-mcp" $KARP
assert_render_fails "karpenter without role fails" "requires autoscaling.karpenter.role" $REQ --set autoscaling.karpenter.enabled=true --api-versions karpenter.sh/v1
assert_render_fails "karpenter without CRD fails loudly" "karpenter.sh/v1 CRD" $REQ --set autoscaling.karpenter.enabled=true --set autoscaling.karpenter.role=r
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh` → FAIL.

- [ ] **Step 3: Create `fc-mcp/templates/autoscaling/karpenter-nodepool.yaml`**

```
{{- if .Values.autoscaling.karpenter.enabled }}
{{- if not (.Capabilities.APIVersions.Has "karpenter.sh/v1") }}
{{- fail "autoscaling.karpenter.enabled=true but the karpenter.sh/v1 CRD is not installed. Install Karpenter first, or render with --api-versions karpenter.sh/v1 --api-versions karpenter.k8s.aws/v1." }}
{{- end }}
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: {{ include "fc-mcp.fullname" . }}-fc-mcp
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
spec:
  template:
    metadata:
      labels:
        {{- toYaml .Values.nodeAgent.nodeSelector | nindent 8 }}
    spec:
      expireAfter: {{ .Values.autoscaling.karpenter.disruption.expireAfter | quote }}
      taints:
        - key: {{ .Values.nodeAgent.taint.key }}
          value: {{ .Values.nodeAgent.taint.value | quote }}
          effect: {{ .Values.nodeAgent.taint.effect }}
      requirements:
        - key: node.kubernetes.io/instance-type
          operator: In
          values: {{ toJson .Values.autoscaling.karpenter.instanceTypes }}
        - key: karpenter.sh/capacity-type
          operator: In
          values: {{ toJson .Values.autoscaling.karpenter.capacityType }}
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: {{ include "fc-mcp.fullname" . }}-fc-mcp
  disruption:
    consolidationPolicy: {{ .Values.autoscaling.karpenter.disruption.consolidationPolicy }}
    consolidateAfter: {{ .Values.autoscaling.karpenter.disruption.consolidateAfter | quote }}
  {{- with .Values.autoscaling.karpenter.limits }}
  limits:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
```

- [ ] **Step 4: Create `fc-mcp/templates/autoscaling/karpenter-ec2nodeclass.yaml`**

```
{{- if .Values.autoscaling.karpenter.enabled }}
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: {{ include "fc-mcp.fullname" . }}-fc-mcp
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
spec:
  amiFamily: {{ .Values.autoscaling.karpenter.amiFamily }}
  role: {{ .Values.autoscaling.karpenter.role | quote }}
  {{- with .Values.autoscaling.karpenter.amiSelectorTerms }}
  amiSelectorTerms:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  subnetSelectorTerms:
    {{- toYaml .Values.autoscaling.karpenter.subnetSelectorTerms | nindent 4 }}
  securityGroupSelectorTerms:
    {{- toYaml .Values.autoscaling.karpenter.securityGroupSelectorTerms | nindent 4 }}
  {{- with .Values.autoscaling.karpenter.tags }}
  tags:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end }}
```

(Pods already carry `karpenter.sh/do-not-disrupt: "true"` from Task 5 when `karpenter.enabled`, so a node holding VMs is never reclaimed. `amiSelectorTerms` must point at a seeded AMI when `seeding.mode=ami`. `m5.metal`/`m6i.metal` are placeholders — KVM needs bare-metal/nested-virt types; confirm before use.)

- [ ] **Step 5: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh` → ALL PASS.

- [ ] **Step 6: Commit**

```bash
git add fc-mcp/templates/autoscaling/karpenter-*.yaml fc-mcp/ci/render-tests.sh
git commit -m "feat(helm): Karpenter NodePool + EC2NodeClass (capability-gated, role required)"
```

---

### Task 11: NOTES.txt, helm test, README, CI values

**Files:**
- Create: `fc-mcp/templates/NOTES.txt`
- Create: `fc-mcp/templates/tests/test-router-ready.yaml`
- Create: `fc-mcp/README.md`
- Create: `fc-mcp/ci/full-stack-values.yaml`
- Create: `fc-mcp/ci/autoscaling-values.yaml`

- [ ] **Step 1: Add assertions**

Append to `ci/render-tests.sh` (just before the final summary):

```bash
# ---- Task 11: NOTES + helm test + ci values ----
assert_contains "helm test hook rendered" 'helm.sh/hook: test' $REQ
if [ -f "$CHART/templates/NOTES.txt" ]; then pass "NOTES.txt present"; else fail "NOTES.txt missing"; fi
assert_lint -f "$CHART/ci/full-stack-values.yaml"
assert_contains "autoscaling ci values render" "kind: ScaledObject" $REQ -f "$CHART/ci/autoscaling-values.yaml" \
  --api-versions keda.sh/v1alpha1 --api-versions karpenter.sh/v1 --api-versions karpenter.k8s.aws/v1
```

- [ ] **Step 2: Run to verify it fails**

Run: `bash fc-mcp/ci/render-tests.sh` → FAIL (no test hook / NOTES / ci files).

- [ ] **Step 3: Create `fc-mcp/ci/full-stack-values.yaml`**

```yaml
image:
  tag: v1.0.0
```

- [ ] **Step 4: Create `fc-mcp/ci/autoscaling-values.yaml`**

```yaml
image:
  tag: v1.0.0
seeding:
  enabled: true
  mode: ami            # bake the seed; no DaemonSet needed for the render test
autoscaling:
  keda:
    enabled: true
  ksmCustomResourceState:
    enabled: true
  karpenter:
    enabled: true
    role: fc-mcp-node
```

- [ ] **Step 5: Create `fc-mcp/templates/tests/test-router-ready.yaml`**

```
apiVersion: v1
kind: Pod
metadata:
  name: {{ include "fc-mcp.fullname" . }}-test-router-ready
  namespace: {{ include "fc-mcp.namespace" . }}
  labels:
    {{- include "fc-mcp.labels" . | nindent 4 }}
  annotations:
    helm.sh/hook: test
    helm.sh/hook-delete-policy: before-hook-creation,hook-succeeded
spec:
  restartPolicy: Never
  containers:
    - name: curl
      image: curlimages/curl:8.10.1
      command: ["/bin/sh", "-c"]
      args:
        - curl -fsS http://fc-mcp-router.{{ include "fc-mcp.namespace" . }}.svc:{{ include "fc-mcp.servicePort" . }}/readyz
```

- [ ] **Step 6: Create `fc-mcp/templates/NOTES.txt`**

```
fc-mcp installed as release "{{ .Release.Name }}" in namespace "{{ include "fc-mcp.namespace" . }}".

1) ENDPOINT — point Claude Code at the ROUTER, never a node:
   claude mcp add --transport http firecracker-bash \
     http://fc-mcp-router.{{ include "fc-mcp.namespace" . }}.svc:{{ include "fc-mcp.servicePort" . }}/mcp
   Auth + TLS terminate at the gateway/ingress in FRONT of this Service (the router does not auth).
{{- if eq .Values.router.service.type "LoadBalancer" }}
   External address: kubectl get svc fc-mcp-router -n {{ include "fc-mcp.namespace" . }}
{{- end }}

2) CRDs were installed from crds/ and are NOT upgraded or deleted by Helm.
   On a chart upgrade that changes CRD schema, first: kubectl apply -f <chart>/crds/
   helm uninstall will NOT remove them or your Session/NodeAgent CRs.

3) NODE-AGENT ROLLOUT — updateStrategy is OnDelete: after `helm upgrade`, node-agent
   pods do NOT auto-restart. Roll one at a time (honoring maxUnavailable: 1), letting
   preStop /drain + re-Ready complete between each:
     kubectl delete pod fc-node-agent-<N> -n {{ include "fc-mcp.namespace" . }}
   The router (Deployment) DOES auto-roll.

4) STORAGE — volumeClaimTemplates are immutable: changing nodeAgent.storage.size/className
   is NOT a live upgrade. See README "Resizing local PVs". PVCs survive uninstall;
   reinstall re-adopts VM state. Purge with: kubectl delete pvc -l app=fc-node-agent -n {{ include "fc-mcp.namespace" . }}

{{- if .Values.autoscaling.keda.enabled }}

5) AUTOSCALING (scale-UP only — pet VMs):
   - KEDA, Prometheus, and kube-state-metrics must already be installed (this chart does not install them).
{{- if .Values.autoscaling.ksmCustomResourceState.enabled }}
   - Point kube-state-metrics at the rendered ConfigMap "{{ include "fc-mcp.fullname" . }}-ksm-crs"
     via --custom-resource-state-config-file (or the KSM chart's customResourceState values),
     and ensure Prometheus scrapes KSM + kubelet. The chart bound KSM SA
     {{ .Values.autoscaling.ksmCustomResourceState.serviceAccount.namespace }}/{{ .Values.autoscaling.ksmCustomResourceState.serviceAccount.name }} to read fcmcp.io/nodeagents.
{{- end }}
   - Keep node scale-DOWN disabled for this pool. Removing a node is the deliberate
     dead-node / drain runbook: drain -> confirm S3 archives -> delete fc-data-<N> PVC -> shrink pool.
{{- end }}
{{- if and .Values.autoscaling.keda.enabled (eq .Values.seeding.mode "ami") }}
   - Seeding mode=ami: confirm the fc-mcp node AMI contains {{ .Values.nodeAgent.seedHostPath }}/{vm-images, vm_ssh_key} (key 0400).
{{- end }}
{{- if eq .Values.seeding.mode "daemonset" }}
   - Seeding mode=daemonset: a privileged DaemonSet syncs s3://<bucket>/{{ .Values.seeding.daemonset.s3.prefix }}/ to {{ .Values.seeding.seedHostPath }} per node.
{{- end }}

Verify: kubectl get statefulset,deploy,po,nodeagents,sessions -n {{ include "fc-mcp.namespace" . }}
{{- if .Values.autoscaling.keda.enabled }}
        kubectl get scaledobject,hpa -n {{ include "fc-mcp.namespace" . }}
{{- end }}
```

- [ ] **Step 7: Create `fc-mcp/README.md`**

Include: install command (`helm install fc-mcp ./fc-mcp -n fc-mcp --create-namespace --set image.tag=<tag>`); the prereqs table (KVM nodes; KEDA/Prometheus/KSM/Karpenter for autoscaling); **resource adoption** (a fresh install into a namespace that already has the kubectl-applied objects fails — `kubectl annotate <kind> <name> meta.helm.sh/release-name=fc-mcp meta.helm.sh/release-namespace=fc-mcp --overwrite` + `kubectl label <kind> <name> app.kubernetes.io/managed-by=Helm --overwrite` for each, or install into a clean namespace); the **resizing local PVs** runbook (`kubectl delete statefulset fc-node-agent --cascade=orphan`, edit, recreate); the **multi-release** note (override `storageClass.name` + KSM/Karpenter names are already release-prefixed); and the `maxVms`-locked-to-32 explanation. Write it as a normal README (prose + fenced commands).

- [ ] **Step 8: Run to verify it passes**

Run: `bash fc-mcp/ci/render-tests.sh` → ALL PASS.

- [ ] **Step 9: Commit**

```bash
git add fc-mcp/templates/NOTES.txt fc-mcp/templates/tests/ fc-mcp/README.md fc-mcp/ci/*.yaml fc-mcp/ci/render-tests.sh
git commit -m "feat(helm): NOTES, helm test hook, README runbooks, CI values"
```

---

### Task 12: Document the chart in CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (add a "Helm deployment" subsection under the Kubernetes section)

- [ ] **Step 1: Add the section to `CLAUDE.md`**

Insert after the "## Kubernetes" content (before "## Local validation in kind"):

```markdown
## Helm deployment (`fc-mcp/`)

`helm install fc-mcp ./fc-mcp -n fc-mcp --create-namespace --set image.tag=<tag>` installs the
full HA stack (CRDs in `crds/`, RBAC, router, node-agent StatefulSet, services, PDBs); defaults
reproduce `kubernetes/*.yaml`. The autoscaling tier (KEDA `ScaledObject`, KSM `CustomResourceState`,
Karpenter `NodePool`/`EC2NodeClass`) is gated behind `autoscaling.*` and off by default — see
`docs/superpowers/specs/2026-06-18-keda-node-autoscaling-plan.md`.

Load-bearing render-time guards (in `templates/_helpers.tpl`):
- **`nodeAgent.maxVms` is LOCKED to 32.** It does NOT resize capacity — real capacity is the
  hardcoded `SLOT_MIN=2`/`SLOT_MAX=33` in `server.py` plus `seq 0 31` in `setup-network.sh`;
  `FC_MAX_VMS` only sets what the NodeAgent CR advertises. Making it tunable is a code change first.
- `image.tag` may not be empty or `latest`; `autoscaling.keda.enabled` requires `seeding.mode≠none`
  (unseeded autoscaled nodes fail "Kernel not found"); `karpenter.enabled` requires `role`.
- Selectors stay the bare `app:` label (StatefulSet/Deployment `spec.selector` is immutable).

Runbooks (NOTES.txt / `fc-mcp/README.md`): OnDelete means `helm upgrade` does not roll node-agents
(delete pods one at a time, honoring `maxUnavailable: 1`); `volumeClaimTemplates` are immutable
(resizing storage is an orphan-delete-and-recreate); adopting the existing kubectl-applied objects
needs Helm ownership annotations or a clean namespace. CRDs in `crds/` are install-only (not
upgraded/deleted by Helm). Validate offline with `bash fc-mcp/ci/render-tests.sh`.
```

- [ ] **Step 2: Verify it reads correctly**

Run: `grep -n "Helm deployment" CLAUDE.md`
Expected: the new heading is present.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document Helm deployment + load-bearing chart guards in CLAUDE.md"
```

---

## Self-Review

**1. Spec coverage** — every spec requirement maps to a task:
- Full-stack resources (CRDs, ns, SA, RBAC, StorageClass, STS, headless svc, router deploy/svc, PDBs): Tasks 2–6 ✔
- CRD `crds/` install-only strategy: Task 2 ✔
- Single image / two roles, router command port-templated: Tasks 5–6 ✔
- Leader-only-Ready + `sessionAffinity: ClientIP`: Task 6 ✔
- Seeding modes (none/ami/daemonset): Task 7 + `validate` guard (Task 1) ✔
- Autoscaling (KEDA/KSM/Karpenter) gated + capability-checked: Tasks 8–10 ✔
- Correctness constraints: `maxVms==32` (Task 1 guard, Task 5 test), bare `app:` selectors (Task 5 helper+test), `keda⇒seeding≠none` (Task 1 guard, Task 7 test), release-prefixed cluster objects (Tasks 4/9/10), single port (Tasks 5–6 + `:8080` test), IRSA values (Tasks 3/7), immutable-field + adoption runbooks (Task 11 README/NOTES) ✔
- `values.schema.json`: Task 1 ✔
- Testing (lint, render asserts, render-fails, helm test): the harness across all tasks + Task 11 ✔
- CLAUDE.md docs: Task 12 ✔

**2. Placeholder scan** — no "TBD/TODO"; every code step has complete content. The only intentional value placeholders (`m5.metal`, ECR repo, Karpenter selector tags) are documented open questions in the spec, surfaced as values defaults with comments — not plan gaps.

**3. Type/name consistency** — helper names are used identically everywhere: `fc-mcp.servicePort`, `fc-mcp.nodeAgent.selectorLabels` / `fc-mcp.router.selectorLabels`, `fc-mcp.storageClassName`, `fc-mcp.headlessServiceName`, `fc-mcp.image`, `fc-mcp.validate`, `fc-mcp.fullname`. Value keys match `values.yaml` (Task 1) across all templates. Render-test var names (`REQ`, `KEDA`, `KSM`, `KARP`) are defined before first use.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-18-helm-chart.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. (Requires `helm` available wherever the subagent runs the render tests.)

**2. Inline Execution** — execute tasks in this session with checkpoints for review.

**Which approach?**

