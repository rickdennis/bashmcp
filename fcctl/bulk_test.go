package main

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// agentWithFlakyDelete: lists two VMs; DELETE of vm-bad returns 500, vm-ok succeeds.
func agentWithFlakyDelete() *httptest.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/vms", func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"vms":[{"vm_id":"vm-ok","name":"ok","status":"running"},{"vm_id":"vm-bad","name":"bad","status":"running"}],"count":2}`)
	})
	mux.HandleFunc("/vms/vm-ok", func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, `{"vm_id":"vm-ok","status":"destroyed"}`)
	})
	mux.HandleFunc("/vms/vm-bad", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
		io.WriteString(w, `{"detail":"kill failed"}`)
	})
	return httptest.NewServer(mux)
}

func TestDestroyAllVMs_CollectsPerItemResults(t *testing.T) {
	srv := agentWithFlakyDelete()
	defer srv.Close()
	c := &NodeClient{BaseURL: srv.URL, Node: "node-a", HTTP: &http.Client{Timeout: 2 * time.Second}}

	results := destroyAllVMs([]*NodeClient{c})

	if len(results) != 2 {
		t.Fatalf("want 2 results, got %d: %+v", len(results), results)
	}
	var okCount, errCount int
	for _, r := range results {
		if r.Err == nil {
			okCount++
		} else {
			errCount++
			if r.VMID != "vm-bad" {
				t.Errorf("unexpected failing vm %q", r.VMID)
			}
		}
	}
	if okCount != 1 || errCount != 1 {
		t.Errorf("want 1 ok + 1 err, got %d ok %d err", okCount, errCount)
	}
}

func TestDestroyAllVMs_NodeListUnreachable(t *testing.T) {
	dead := &NodeClient{BaseURL: "http://127.0.0.1:1", Node: "node-dead", HTTP: &http.Client{Timeout: 300 * time.Millisecond}}
	results := destroyAllVMs([]*NodeClient{dead})
	// An unreachable node yields a single node-level error result, not a panic.
	if len(results) != 1 || results[0].Err == nil {
		t.Fatalf("want 1 node-level error result, got %+v", results)
	}
	if results[0].Node != "node-dead" {
		t.Errorf("want node-dead, got %q", results[0].Node)
	}
}
