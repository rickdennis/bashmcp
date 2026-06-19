// fc-agent — guest-side command-execution agent for the fc-mcp microVM stack.
//
// The host (the bridge gateway, 172.16.0.1) drives commands into the VM over this
// agent instead of SSH. P0/P1 expose an HTTP control plane:
//
//	GET  /health   liveness (source-IP gated, no token) — used by the host readiness wait
//	POST /exec     run a command (source-IP gated + bearer token) -> {stdout,stderr,returncode}
//
// The WebSocket data plane (:2024) for streaming/reattachable exec lands in P2.
//
// Security: only accepts connections from --allow-from (the host gateway), and /exec
// additionally requires the per-VM bearer token written into the overlay at VM-create.
package main

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

var (
	httpPort  = flag.String("listen-http", "2025", "control-plane HTTP port")
	wsPort    = flag.String("listen-ws", "2024", "data-plane WebSocket port (reserved; unused until P2)")
	allowFrom = flag.String("allow-from", "172.16.0.1", "only accept connections from this source IP (the host gateway)")
	tokenFile = flag.String("token-file", "/etc/fc-agent/token", "bearer-token file; /exec is disabled until this exists")
)

var authToken string // loaded at startup; empty => /exec returns 503 (not yet provisioned)

type execRequest struct {
	Command     string `json:"command"`
	TimeoutSecs int    `json:"timeout_secs"`
	WorkingDir  string `json:"working_dir"`
	ClearEnv    bool   `json:"clear_env"`
}

type execResponse struct {
	Stdout     string `json:"stdout"`
	Stderr     string `json:"stderr"`
	ReturnCode int    `json:"returncode"`
}

func main() {
	flag.Parse()
	if b, err := os.ReadFile(*tokenFile); err == nil {
		authToken = strings.TrimSpace(string(b))
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/health", withSourceIP(handleHealth))
	mux.HandleFunc("/exec", withSourceIP(withToken(handleExec)))

	addr := ":" + *httpPort
	log.Printf("fc-agent: control plane on %s (ws %s reserved), allow-from=%s, exec-ready=%v",
		addr, *wsPort, *allowFrom, authToken != "")
	log.Fatal((&http.Server{Addr: addr, Handler: mux}).ListenAndServe())
}

// withSourceIP rejects any request whose source IP is not the allowed host gateway.
// Under the per-VM tap topology the host (172.16.0.1) is the only other party on the
// segment, so this is the analog of the reference design's --block-local-connections.
func withSourceIP(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		host, _, err := net.SplitHostPort(r.RemoteAddr)
		if err != nil || host != *allowFrom {
			log.Printf("[SECURITY] rejected %s %s from %s", r.Method, r.URL.Path, r.RemoteAddr)
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		next(w, r)
	}
}

// withToken enforces the per-VM bearer token. 503 until a token has been provisioned
// into the overlay (lets /health-based readiness succeed before the token exists).
func withToken(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if authToken == "" {
			http.Error(w, "no token configured", http.StatusServiceUnavailable)
			return
		}
		got := strings.TrimSpace(strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer "))
		if subtle.ConstantTimeCompare([]byte(got), []byte(authToken)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		next(w, r)
	}
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte("ok"))
}

func handleExec(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	var req execRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Command == "" {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	timeout := time.Duration(req.TimeoutSecs) * time.Second
	if timeout <= 0 {
		timeout = 60 * time.Second
	}
	resp := runCommand(req, timeout)
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(resp)
}

// runCommand runs the command in its own process group so a timeout kills the whole
// tree (SSH only killed the client, leaving the remote command running — this is the
// intended improvement). Returns captured output and the exit code.
func runCommand(req execRequest, timeout time.Duration) execResponse {
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	cmd := exec.Command("/bin/bash", "-lc", req.Command)
	if req.WorkingDir != "" {
		cmd.Dir = req.WorkingDir
	}
	if req.ClearEnv {
		cmd.Env = []string{"PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}

	var outBuf, errBuf bytes.Buffer
	cmd.Stdout = &outBuf
	cmd.Stderr = &errBuf

	if err := cmd.Start(); err != nil {
		return execResponse{Stderr: "failed to start command: " + err.Error(), ReturnCode: -1}
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	select {
	case <-ctx.Done():
		if cmd.Process != nil {
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL) // kill the process group
		}
		<-done
		errOut := errBuf.String()
		if errOut != "" && !strings.HasSuffix(errOut, "\n") {
			errOut += "\n"
		}
		return execResponse{Stdout: outBuf.String(), Stderr: errOut + "command timed out", ReturnCode: -1}
	case err := <-done:
		rc := 0
		if err != nil {
			if ee, ok := err.(*exec.ExitError); ok {
				rc = ee.ExitCode()
			} else {
				rc = -1
				errBuf.WriteString("\n" + err.Error())
			}
		}
		return execResponse{Stdout: outBuf.String(), Stderr: errBuf.String(), ReturnCode: rc}
	}
}
