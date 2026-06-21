package main

import (
	"testing"
	"time"
)

// Regression for the "apt-get update -> context deadline exceeded" bug: exec's HTTP
// client ceiling must exceed the in-VM --timeout, or long commands die at the
// transport layer before the server's own SSH timeout can apply.
func TestExecClientTimeoutExceedsExecTimeout(t *testing.T) {
	for _, execT := range []int{60, 120, 600} {
		got := execClientTimeout(execT)
		if got <= time.Duration(execT)*time.Second {
			t.Errorf("execClientTimeout(%d) = %v, want strictly greater than %ds", execT, got, execT)
		}
	}
}

// opClient must build a client to the resolved node's podIP with the timeout the
// operation asked for — NOT the short discovery timeout reused from the gather.
func TestOpClientUsesGivenTimeout(t *testing.T) {
	var g Gather
	g.Port = 8080
	na := nodeAgent("w1", "Ready", 5)
	na.Spec.PodIP = "172.18.0.3"
	g.NodeAgents = []NodeAgent{na}

	c := g.opClient("w1", 90*time.Second)
	if c == nil {
		t.Fatal("opClient returned nil for a known node")
	}
	if c.HTTP.Timeout != 90*time.Second {
		t.Errorf("opClient timeout = %v, want 90s", c.HTTP.Timeout)
	}
	if c.BaseURL != "http://172.18.0.3:8080" {
		t.Errorf("opClient baseURL = %q", c.BaseURL)
	}
	if g.opClient("nope", time.Second) != nil {
		t.Error("opClient should be nil for an unknown node")
	}
}

func TestResolveNode(t *testing.T) {
	var g Gather
	g.VMsByNode = map[string][]VM{"w1": {{VMID: "vm-1"}}, "w2": {{VMID: "vm-2"}}}
	if node, ok := g.resolveNode("vm-2"); !ok || node != "w2" {
		t.Errorf("resolveNode(vm-2) = %q,%v", node, ok)
	}
	if _, ok := g.resolveNode("ghost"); ok {
		t.Error("resolveNode should miss an unknown vm")
	}
}
