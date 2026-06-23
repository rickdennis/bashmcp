package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// ─── VM data model (mirrors the node-agent JSON) ───────────────────────────────

type VM struct {
	VMID        string  `json:"vm_id"`
	Name        string  `json:"name"`
	Status      string  `json:"status"`
	VCPU        int     `json:"vcpu"`
	MemMB       int     `json:"mem_mb"`
	IPAddress   string  `json:"ip_address"`
	HasSnapshot bool    `json:"has_snapshot"`
	CreatedAt   float64 `json:"created_at"`
	Node        string  `json:"-"` // filled in by the client from which node answered
}

type Snapshot struct {
	CreatedAt float64 `json:"created_at"`
	MemSizeMB float64 `json:"mem_size_mb"`
}

type VMDetail struct {
	VMID      string    `json:"vm_id"`
	Name      string    `json:"name"`
	Status    string    `json:"status"`
	VCPU      int       `json:"vcpu"`
	MemMB     int       `json:"mem_mb"`
	DiskMB    int       `json:"disk_mb"`
	IPAddress string    `json:"ip_address"`
	PID       int       `json:"pid"`
	CreatedAt float64   `json:"created_at"`
	Snapshot  *Snapshot `json:"snapshot"`
	Error     string    `json:"error"`
	Node      string    `json:"-"`
}

type ExecResult struct {
	VMID           string  `json:"vm_id"`
	Command        string  `json:"command"`
	Stdout         string  `json:"stdout"`
	Stderr         string  `json:"stderr"`
	ReturnCode     int     `json:"returncode"`
	ElapsedSeconds float64 `json:"elapsed_seconds"`
}

type CreateReq struct {
	Name   string `json:"name,omitempty"`
	VCPU   int    `json:"vcpu,omitempty"`
	MemMB  int    `json:"mem_mb,omitempty"`
	DiskMB int    `json:"disk_mb,omitempty"`
}

// ─── node-agent REST client ────────────────────────────────────────────────────

type NodeClient struct {
	BaseURL string
	Node    string // node name this agent runs on (for stamping/messages)
	HTTP    *http.Client
}

func newNodeClient(node, podIP string, port int, timeout time.Duration) *NodeClient {
	return &NodeClient{
		BaseURL: fmt.Sprintf("http://%s:%d", podIP, port),
		Node:    node,
		HTTP:    &http.Client{Timeout: timeout},
	}
}

// do issues a request and returns the body, erroring (with the server's `detail`
// when present) on any non-2xx status.
func (c *NodeClient) do(method, path string, body any) ([]byte, error) {
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequest(method, c.BaseURL+path, rdr)
	if err != nil {
		return nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("%s %s: %s", method, path, errDetail(data, resp.StatusCode))
	}
	return data, nil
}

// errDetail pulls FastAPI's {"detail": ...} or {"error": ...} out of an error body.
func errDetail(data []byte, code int) string {
	var e struct {
		Detail any    `json:"detail"`
		Error  string `json:"error"`
	}
	if json.Unmarshal(data, &e) == nil {
		if s, ok := e.Detail.(string); ok && s != "" {
			return s
		}
		if e.Error != "" {
			return e.Error
		}
	}
	return fmt.Sprintf("HTTP %d", code)
}

func (c *NodeClient) ListVMs() ([]VM, error) {
	data, err := c.do(http.MethodGet, "/vms", nil)
	if err != nil {
		return nil, err
	}
	var wrap struct {
		VMs []VM `json:"vms"`
	}
	if err := json.Unmarshal(data, &wrap); err != nil {
		return nil, err
	}
	for i := range wrap.VMs {
		wrap.VMs[i].Node = c.Node
	}
	return wrap.VMs, nil
}

func (c *NodeClient) GetVM(id string) (*VMDetail, error) {
	data, err := c.do(http.MethodGet, "/vms/"+id, nil)
	if err != nil {
		return nil, err
	}
	var d VMDetail
	if err := json.Unmarshal(data, &d); err != nil {
		return nil, err
	}
	d.Node = c.Node
	return &d, nil
}

func (c *NodeClient) Exec(id, command, workdir string, timeout int) (*ExecResult, error) {
	body := map[string]any{"command": command, "timeout": timeout}
	if workdir != "" {
		body["working_dir"] = workdir
	}
	data, err := c.do(http.MethodPost, "/vms/"+id+"/exec", body)
	if err != nil {
		return nil, err
	}
	var res ExecResult
	if err := json.Unmarshal(data, &res); err != nil {
		return nil, err
	}
	return &res, nil
}

func (c *NodeClient) Create(req CreateReq) (*VM, error) {
	data, err := c.do(http.MethodPost, "/vms", req)
	if err != nil {
		return nil, err
	}
	var vm VM
	if err := json.Unmarshal(data, &vm); err != nil {
		return nil, err
	}
	vm.Node = c.Node
	return &vm, nil
}

func (c *NodeClient) Pause(id string) error {
	_, err := c.do(http.MethodPost, "/vms/"+id+"/pause", nil)
	return err
}

func (c *NodeClient) Resume(id string) error {
	_, err := c.do(http.MethodPost, "/vms/"+id+"/resume", nil)
	return err
}

func (c *NodeClient) Destroy(id string) error {
	_, err := c.do(http.MethodDelete, "/vms/"+id, nil)
	return err
}

func (c *NodeClient) Restore(vmID, sessionID string) error {
	body := map[string]any{"vm_id": vmID}
	if sessionID != "" {
		body["session_id"] = sessionID
	}
	_, err := c.do(http.MethodPost, "/restore", body)
	return err
}

func (c *NodeClient) Drain() ([]byte, error) {
	return c.do(http.MethodPost, "/drain", nil)
}
