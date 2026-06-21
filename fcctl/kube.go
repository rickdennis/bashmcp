package main

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// ─── CR types (subset of the live JSON we care about) ──────────────────────────

type NodeAgent struct {
	Meta struct {
		Name string `json:"name"`
	} `json:"metadata"`
	Spec struct {
		NodeName string `json:"nodeName"`
		PodIP    string `json:"podIP"`
		MaxVMs   int    `json:"maxVms"`
	} `json:"spec"`
	Status struct {
		Phase         string `json:"phase"`
		FreeTaps      int    `json:"freeTaps"`
		HeartbeatTime string `json:"heartbeatTime"`
	} `json:"status"`
}

type Session struct {
	Meta struct {
		Name              string `json:"name"`
		CreationTimestamp string `json:"creationTimestamp"`
	} `json:"metadata"`
	Spec struct {
		McpSessionID string `json:"mcpSessionId"`
		NodeName     string `json:"nodeName"`
	} `json:"spec"`
	Status struct {
		Phase string `json:"phase"`
		VMRef string `json:"vmRef"`
	} `json:"status"`
}

func parseNodeAgents(b []byte) ([]NodeAgent, error) {
	var list struct {
		Items []NodeAgent `json:"items"`
	}
	if err := json.Unmarshal(b, &list); err != nil {
		return nil, err
	}
	return list.Items, nil
}

func parseSessions(b []byte) ([]Session, error) {
	var list struct {
		Items []Session `json:"items"`
	}
	if err := json.Unmarshal(b, &list); err != nil {
		return nil, err
	}
	return list.Items, nil
}

func parseLeaseHolder(b []byte) (string, error) {
	var lease struct {
		Spec struct {
			HolderIdentity string `json:"holderIdentity"`
		} `json:"spec"`
	}
	if err := json.Unmarshal(b, &lease); err != nil {
		return "", err
	}
	return lease.Spec.HolderIdentity, nil
}

// ─── kubectl shell wrapper ─────────────────────────────────────────────────────

// Kube shells out to kubectl, ALWAYS pinning --kubeconfig explicitly (never ambient)
// so behavior is deterministic across users and sudo (where HOME differs).
type Kube struct {
	Bin        string
	Kubeconfig string
	Namespace  string
}

func (k *Kube) run(args ...string) ([]byte, error) {
	full := append([]string{"--kubeconfig", k.Kubeconfig, "-n", k.Namespace}, args...)
	cmd := exec.Command(k.Bin, full...)
	var out, errb strings.Builder
	cmd.Stdout = &out
	cmd.Stderr = &errb
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(errb.String())
		if lines := strings.Split(msg, "\n"); len(lines) > 0 && lines[len(lines)-1] != "" {
			msg = lines[len(lines)-1]
		}
		if msg == "" {
			msg = err.Error()
		}
		return nil, fmt.Errorf("kubectl %s: %s", strings.Join(args, " "), msg)
	}
	return []byte(out.String()), nil
}

func (k *Kube) NodeAgents() ([]NodeAgent, error) {
	b, err := k.run("get", "nodeagents", "-o", "json")
	if err != nil {
		return nil, err
	}
	return parseNodeAgents(b)
}

func (k *Kube) Sessions() ([]Session, error) {
	b, err := k.run("get", "sessions", "-o", "json")
	if err != nil {
		return nil, err
	}
	return parseSessions(b)
}

func (k *Kube) LeaseHolder(lease string) (string, error) {
	b, err := k.run("get", "lease", lease, "-o", "json")
	if err != nil {
		return "", err
	}
	return parseLeaseHolder(b)
}

// DeleteAllSessions removes every Session CR (used by `reset`). Returns kubectl's
// combined message for reporting.
func (k *Kube) DeleteAllSessions() (string, error) {
	b, err := k.run("delete", "sessions", "--all")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(b)), nil
}

// resolveKubeconfig picks the kubeconfig path: explicit flag > KUBECONFIG env >
// ~/.kube/config. It errors if the resolved file does not exist.
func resolveKubeconfig(flag string) (string, error) {
	path := flag
	if path == "" {
		path = os.Getenv("KUBECONFIG")
	}
	if path == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", fmt.Errorf("cannot determine home dir for default kubeconfig: %w", err)
		}
		path = home + "/.kube/config"
	}
	// KUBECONFIG may be a list; take the first entry.
	if i := strings.IndexByte(path, os.PathListSeparator); i >= 0 {
		path = path[:i]
	}
	if _, err := os.Stat(path); err != nil {
		return "", fmt.Errorf("kubeconfig not found at %s", path)
	}
	return path, nil
}
