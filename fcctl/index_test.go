package main

import (
	"net/http"
	"testing"
	"time"
)

func TestFanoutVMs(t *testing.T) {
	srv := fakeAgent(t)
	defer srv.Close()

	good := newTestClient(srv.URL)
	good.Node = "node-a"
	// A client pointed at a dead address — must be marked unreachable, not fatal.
	dead := &NodeClient{BaseURL: "http://127.0.0.1:1", Node: "node-b", HTTP: &http.Client{Timeout: 300 * time.Millisecond}}

	vmsByNode, reachable := fanoutVMs([]*NodeClient{good, dead})

	if !reachable["node-a"] {
		t.Errorf("node-a should be reachable")
	}
	if reachable["node-b"] {
		t.Errorf("node-b should be unreachable")
	}
	if len(vmsByNode["node-a"]) != 2 {
		t.Errorf("want 2 vms on node-a, got %d", len(vmsByNode["node-a"]))
	}
	if len(vmsByNode["node-b"]) != 0 {
		t.Errorf("want 0 vms on node-b, got %d", len(vmsByNode["node-b"]))
	}
}

func TestIndexVMs(t *testing.T) {
	vmsByNode := map[string][]VM{
		"node-a": {{VMID: "vm-1"}, {VMID: "vm-2"}},
		"node-b": {{VMID: "vm-3"}},
	}
	idx := indexVMs(vmsByNode)
	if idx["vm-1"] != "node-a" || idx["vm-3"] != "node-b" {
		t.Errorf("index mismatch: %+v", idx)
	}
	if _, ok := idx["nope"]; ok {
		t.Errorf("unexpected entry for missing vm")
	}
}

func nodeAgent(name string, phase string, free int) NodeAgent {
	var n NodeAgent
	n.Spec.NodeName = name
	n.Spec.MaxVMs = 32
	n.Status.Phase = phase
	n.Status.FreeTaps = free
	return n
}

func TestSelectNodeForCreate_MostFree(t *testing.T) {
	nas := []NodeAgent{
		nodeAgent("worker-1", "Ready", 5),
		nodeAgent("worker-2", "Ready", 20),
		nodeAgent("worker-3", "Ready", 12),
	}
	n, err := selectNodeForCreate(nas)
	if err != nil {
		t.Fatalf("selectNodeForCreate: %v", err)
	}
	if n.Spec.NodeName != "worker-2" {
		t.Errorf("want worker-2 (most free), got %s", n.Spec.NodeName)
	}
}

func TestSelectNodeForCreate_TieByName(t *testing.T) {
	nas := []NodeAgent{
		nodeAgent("worker-9", "Ready", 10),
		nodeAgent("worker-2", "Ready", 10),
	}
	n, err := selectNodeForCreate(nas)
	if err != nil {
		t.Fatalf("selectNodeForCreate: %v", err)
	}
	if n.Spec.NodeName != "worker-2" {
		t.Errorf("tie should break to lowest name worker-2, got %s", n.Spec.NodeName)
	}
}

func TestSelectNodeForCreate_SkipsNotReadyAndFull(t *testing.T) {
	nas := []NodeAgent{
		nodeAgent("worker-1", "NotReady", 30), // most free but not Ready
		nodeAgent("worker-2", "Ready", 0),      // Ready but full
		nodeAgent("worker-3", "Ready", 3),      // the only viable one
	}
	n, err := selectNodeForCreate(nas)
	if err != nil {
		t.Fatalf("selectNodeForCreate: %v", err)
	}
	if n.Spec.NodeName != "worker-3" {
		t.Errorf("want worker-3, got %s", n.Spec.NodeName)
	}
}

func TestSelectNodeForCreate_NoneViable(t *testing.T) {
	nas := []NodeAgent{
		nodeAgent("worker-1", "Ready", 0),
		nodeAgent("worker-2", "NotReady", 10),
	}
	if _, err := selectNodeForCreate(nas); err == nil {
		t.Fatal("expected error when no Ready node has capacity")
	}
}

// findNode is the host:port helper that maps a NodeAgent to a NodeClient.
func TestClientsFromNodeAgents(t *testing.T) {
	nas := []NodeAgent{
		nodeAgent("worker-1", "Ready", 5),
	}
	nas[0].Spec.PodIP = "172.18.0.3"
	clients := clientsFromNodeAgents(nas, 8080, time.Second)
	if len(clients) != 1 {
		t.Fatalf("want 1 client, got %d", len(clients))
	}
	if clients[0].Node != "worker-1" || clients[0].BaseURL != "http://172.18.0.3:8080" {
		t.Errorf("client mismatch: %+v", clients[0])
	}
}
