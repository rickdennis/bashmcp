package main

import "testing"

// Recorded shapes from `kubectl get nodeagents/sessions/lease -n fc-mcp -o json`.
const nodeAgentsJSON = `{
  "items": [
    {
      "metadata": {"name": "fc-mcp-node-0"},
      "spec": {"nodeName": "kind-worker", "podIP": "172.18.0.3", "maxVms": 32},
      "status": {"phase": "Ready", "freeTaps": 30, "heartbeatTime": "2026-06-18T11:00:00Z"}
    },
    {
      "metadata": {"name": "fc-mcp-node-1"},
      "spec": {"nodeName": "kind-worker2", "podIP": "172.18.0.4", "maxVms": 32},
      "status": {"phase": "NotReady", "freeTaps": 32, "heartbeatTime": "2026-06-18T10:59:00Z"}
    }
  ]
}`

const sessionsJSON = `{
  "items": [
    {
      "metadata": {"name": "s-abc123", "creationTimestamp": "2026-06-18T10:55:00Z"},
      "spec": {"mcpSessionId": "abc123def456", "nodeName": "kind-worker"},
      "status": {"phase": "Bound", "vmRef": ""}
    }
  ]
}`

const leaseJSON = `{
  "metadata": {"name": "fc-mcp-router-leader"},
  "spec": {"holderIdentity": "router-7d9f-xyz"}
}`

func TestParseNodeAgents(t *testing.T) {
	nas, err := parseNodeAgents([]byte(nodeAgentsJSON))
	if err != nil {
		t.Fatalf("parseNodeAgents: %v", err)
	}
	if len(nas) != 2 {
		t.Fatalf("want 2 nodeagents, got %d", len(nas))
	}
	n := nas[0]
	if n.Spec.NodeName != "kind-worker" || n.Spec.PodIP != "172.18.0.3" || n.Spec.MaxVMs != 32 {
		t.Errorf("spec mismatch: %+v", n.Spec)
	}
	if n.Status.Phase != "Ready" || n.Status.FreeTaps != 30 {
		t.Errorf("status mismatch: %+v", n.Status)
	}
	if n.Status.HeartbeatTime != "2026-06-18T11:00:00Z" {
		t.Errorf("heartbeat mismatch: %q", n.Status.HeartbeatTime)
	}
}

func TestParseSessions(t *testing.T) {
	ss, err := parseSessions([]byte(sessionsJSON))
	if err != nil {
		t.Fatalf("parseSessions: %v", err)
	}
	if len(ss) != 1 {
		t.Fatalf("want 1 session, got %d", len(ss))
	}
	s := ss[0]
	if s.Meta.Name != "s-abc123" || s.Spec.McpSessionID != "abc123def456" {
		t.Errorf("session mismatch: %+v %+v", s.Meta, s.Spec)
	}
	if s.Spec.NodeName != "kind-worker" || s.Status.Phase != "Bound" {
		t.Errorf("session status mismatch: %+v %+v", s.Spec, s.Status)
	}
	if s.Status.VMRef != "" {
		t.Errorf("expected empty vmRef, got %q", s.Status.VMRef)
	}
}

func TestParseLeaseHolder(t *testing.T) {
	holder, err := parseLeaseHolder([]byte(leaseJSON))
	if err != nil {
		t.Fatalf("parseLeaseHolder: %v", err)
	}
	if holder != "router-7d9f-xyz" {
		t.Errorf("holder mismatch: %q", holder)
	}
}

func TestParseLeaseHolderMissing(t *testing.T) {
	holder, err := parseLeaseHolder([]byte(`{"metadata":{"name":"x"}}`))
	if err != nil {
		t.Fatalf("parseLeaseHolder on holderless lease should not error: %v", err)
	}
	if holder != "" {
		t.Errorf("want empty holder, got %q", holder)
	}
}
