package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// fakeAgent stands in for a node-agent's REST surface.
func fakeAgent(t *testing.T) *httptest.Server {
	mux := http.NewServeMux()
	mux.HandleFunc("/vms", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost { // create
			w.WriteHeader(201)
			io.WriteString(w, `{"vm_id":"vm-new","name":"vm-new","status":"running","ip_address":"172.16.0.5","vcpu":1,"mem_mb":512,"disk_mb":2048}`)
			return
		}
		io.WriteString(w, `{"vms":[
		  {"vm_id":"vm-aaa","name":"alpha","status":"running","vcpu":1,"mem_mb":512,"ip_address":"172.16.0.2","has_snapshot":false,"created_at":1750000000.0},
		  {"vm_id":"vm-bbb","name":"beta","status":"paused","vcpu":2,"mem_mb":1024,"ip_address":"172.16.0.3","has_snapshot":true,"created_at":1750000100.0}
		],"count":2}`)
	})
	mux.HandleFunc("/vms/vm-aaa", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodDelete {
			io.WriteString(w, `{"vm_id":"vm-aaa","name":"alpha","status":"destroyed"}`)
			return
		}
		io.WriteString(w, `{"vm_id":"vm-aaa","name":"alpha","status":"running","vcpu":1,"mem_mb":512,"disk_mb":2048,"ip_address":"172.16.0.2","pid":1234,"created_at":1750000000.0,"snapshot":null,"error":null}`)
	})
	mux.HandleFunc("/vms/vm-aaa/exec", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		json.NewDecoder(r.Body).Decode(&body)
		if body["command"] != "echo hi" {
			t.Errorf("exec got command %q", body["command"])
		}
		io.WriteString(w, `{"vm_id":"vm-aaa","command":"echo hi","stdout":"hi\n","stderr":"","returncode":0,"elapsed_seconds":0.1}`)
	})
	mux.HandleFunc("/vms/vm-missing", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(404)
		io.WriteString(w, `{"detail":"VM 'vm-missing' not found."}`)
	})
	return httptest.NewServer(mux)
}

func newTestClient(base string) *NodeClient {
	return &NodeClient{BaseURL: base, Node: "test-node", HTTP: &http.Client{Timeout: 5 * time.Second}}
}

func TestListVMs(t *testing.T) {
	srv := fakeAgent(t)
	defer srv.Close()
	vms, err := newTestClient(srv.URL).ListVMs()
	if err != nil {
		t.Fatalf("ListVMs: %v", err)
	}
	if len(vms) != 2 {
		t.Fatalf("want 2 vms, got %d", len(vms))
	}
	if vms[0].VMID != "vm-aaa" || vms[0].Name != "alpha" || vms[0].Status != "running" {
		t.Errorf("vm0 mismatch: %+v", vms[0])
	}
	if vms[1].HasSnapshot != true || vms[1].MemMB != 1024 {
		t.Errorf("vm1 mismatch: %+v", vms[1])
	}
	// Node is stamped by the client so callers know who answered.
	if vms[0].Node != "test-node" {
		t.Errorf("want Node stamped, got %q", vms[0].Node)
	}
}

func TestGetVM(t *testing.T) {
	srv := fakeAgent(t)
	defer srv.Close()
	d, err := newTestClient(srv.URL).GetVM("vm-aaa")
	if err != nil {
		t.Fatalf("GetVM: %v", err)
	}
	if d.PID != 1234 || d.DiskMB != 2048 || d.Status != "running" {
		t.Errorf("detail mismatch: %+v", d)
	}
}

func TestGetVMNotFound(t *testing.T) {
	srv := fakeAgent(t)
	defer srv.Close()
	_, err := newTestClient(srv.URL).GetVM("vm-missing")
	if err == nil {
		t.Fatal("expected error for 404")
	}
	if !strings.Contains(err.Error(), "not found") {
		t.Errorf("want detail surfaced, got %v", err)
	}
}

func TestExec(t *testing.T) {
	srv := fakeAgent(t)
	defer srv.Close()
	res, err := newTestClient(srv.URL).Exec("vm-aaa", "echo hi", "", 60)
	if err != nil {
		t.Fatalf("Exec: %v", err)
	}
	if res.Stdout != "hi\n" || res.ReturnCode != 0 {
		t.Errorf("exec result mismatch: %+v", res)
	}
}

func TestDestroy(t *testing.T) {
	srv := fakeAgent(t)
	defer srv.Close()
	if err := newTestClient(srv.URL).Destroy("vm-aaa"); err != nil {
		t.Fatalf("Destroy: %v", err)
	}
}

func TestCreate(t *testing.T) {
	srv := fakeAgent(t)
	defer srv.Close()
	vm, err := newTestClient(srv.URL).Create(CreateReq{Name: "vm-new", VCPU: 1, MemMB: 512, DiskMB: 2048})
	if err != nil {
		t.Fatalf("Create: %v", err)
	}
	if vm.VMID != "vm-new" || vm.Status != "running" {
		t.Errorf("create result mismatch: %+v", vm)
	}
}
