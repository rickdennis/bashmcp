// fc-agent — guest-side command-execution agent for the fc-mcp microVM stack.
//
// The host (the bridge gateway, 172.16.0.1) drives commands into the VM over this
// agent instead of SSH. HTTP control plane:
//
//	GET    /health                 liveness (source-IP gated, no token)
//	POST   /exec                   one-shot synchronous run -> {stdout,stderr,returncode}
//	POST   /exec_async             start a tmux-backed run -> {exec_id} (long-running/reattachable)
//	GET    /exec_async/{id}?out=&err=   poll output from offsets -> {stdout,stderr,out_offset,err_offset,done,returncode}
//	DELETE /exec_async/{id}        kill the tmux session + remove temp files
//
// The async model owns a command's lifetime in tmux + on-disk files keyed by exec_id,
// so a command survives a host reconnect AND a VM pause/resume: the host just re-polls
// the same exec_id from its last offset. This mirrors the reference design's tmux +
// `.done` sentinel mechanism. The WS data plane port (:2024) is reserved, not yet used.
//
// Security: only accepts connections from --allow-from (the host gateway); every
// endpoint except /health additionally requires the per-VM bearer token.
package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var (
	httpPort  = flag.String("listen-http", "2025", "control-plane HTTP port")
	wsPort    = flag.String("listen-ws", "2024", "data-plane WebSocket port (reserved; unused)")
	allowFrom = flag.String("allow-from", "172.16.0.1", "only accept connections from this source IP (the host gateway)")
	tokenFile = flag.String("token-file", "/etc/fc-agent/token", "bearer-token file; non-/health endpoints disabled until this exists")
)

var authToken string // loaded at startup; empty => token-gated endpoints return 503

const tmpPrefix = "/tmp/fc-" // per-exec scratch: <prefix><id>.{cmd,sh,out,err,done}

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
	mux.HandleFunc("POST /exec", withSourceIP(withToken(handleExec)))
	mux.HandleFunc("POST /exec_async", withSourceIP(withToken(handleExecAsyncStart)))
	mux.HandleFunc("GET /exec_async/{id}", withSourceIP(withToken(handleExecAsyncPoll)))
	mux.HandleFunc("DELETE /exec_async/{id}", withSourceIP(withToken(handleExecAsyncDelete)))

	addr := ":" + *httpPort
	log.Printf("fc-agent: control plane on %s (ws %s reserved), allow-from=%s, exec-ready=%v",
		addr, *wsPort, *allowFrom, authToken != "")
	log.Fatal((&http.Server{Addr: addr, Handler: mux}).ListenAndServe())
}

// withSourceIP rejects any request whose source IP is not the allowed host gateway —
// the analog of the reference design's --block-local-connections.
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

// withToken enforces the per-VM bearer token. 503 until a token is provisioned.
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

// ── synchronous one-shot exec (kept for short commands) ──────────────────────

func handleExec(w http.ResponseWriter, r *http.Request) {
	var req execRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Command == "" {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	timeout := time.Duration(reqTimeout(req)) * time.Second
	writeJSON(w, runCommand(req, timeout))
}

// runCommand runs the command in its own process group so a timeout kills the whole tree.
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
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
		<-done
		errOut := errBuf.String()
		if errOut != "" && !strings.HasSuffix(errOut, "\n") {
			errOut += "\n"
		}
		return execResponse{Stdout: outBuf.String(), Stderr: errOut + "command timed out", ReturnCode: -1}
	case err := <-done:
		return execResponse{Stdout: outBuf.String(), Stderr: errBuf.String(), ReturnCode: exitCode(err, &errBuf)}
	}
}

// ── async (tmux-backed) exec: long-running, reattachable, pause/resume-safe ──

func handleExecAsyncStart(w http.ResponseWriter, r *http.Request) {
	var req execRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil || req.Command == "" {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	id, err := randID()
	if err != nil {
		http.Error(w, "id error", http.StatusInternalServerError)
		return
	}
	cmdPath := tmpPrefix + id + ".cmd"
	shPath := tmpPrefix + id + ".sh"
	outPath := tmpPrefix + id + ".out"
	errPath := tmpPrefix + id + ".err"
	donePath := tmpPrefix + id + ".done"

	if err := os.WriteFile(cmdPath, []byte(req.Command), 0o600); err != nil {
		http.Error(w, "write cmd: "+err.Error(), http.StatusInternalServerError)
		return
	}
	// Wrapper: redirect all output to the .out/.err files, run the command under a
	// timeout, then record the exit code in the .done sentinel. The host polls these.
	var sb strings.Builder
	sb.WriteString("#!/bin/bash\n")
	sb.WriteString(fmt.Sprintf("exec > %s 2> %s\n", outPath, errPath))
	if req.WorkingDir != "" {
		sb.WriteString(fmt.Sprintf("cd %s || { echo 'cd failed' >&2; echo 127 > %s; exit; }\n", shQuote(req.WorkingDir), donePath))
	}
	sb.WriteString(fmt.Sprintf("timeout %d bash %s\n", reqTimeout(req), cmdPath))
	sb.WriteString(fmt.Sprintf("echo $? > %s\n", donePath))
	if err := os.WriteFile(shPath, []byte(sb.String()), 0o700); err != nil {
		http.Error(w, "write wrapper: "+err.Error(), http.StatusInternalServerError)
		return
	}
	// Detached tmux session owns the command's lifetime (survives this connection,
	// and is captured in a VM snapshot for pause/resume).
	launch := exec.Command("tmux", "new-session", "-d", "-s", "fc-"+id, "bash "+shPath)
	if out, err := launch.CombinedOutput(); err != nil {
		http.Error(w, "tmux launch failed: "+err.Error()+": "+string(out), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]string{"exec_id": id})
}

func handleExecAsyncPoll(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validID(id) {
		http.Error(w, "bad id", http.StatusBadRequest)
		return
	}
	outOff := queryInt(r, "out")
	errOff := queryInt(r, "err")
	outChunk, newOut := readFrom(tmpPrefix+id+".out", outOff)
	errChunk, newErr := readFrom(tmpPrefix+id+".err", errOff)

	done := false
	rc := -1
	if b, err := os.ReadFile(tmpPrefix + id + ".done"); err == nil {
		done = true
		if n, e := strconv.Atoi(strings.TrimSpace(string(b))); e == nil {
			rc = n
		}
	}
	writeJSON(w, map[string]any{
		"stdout": outChunk, "stderr": errChunk,
		"out_offset": newOut, "err_offset": newErr,
		"done": done, "returncode": rc,
	})
}

func handleExecAsyncDelete(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !validID(id) {
		http.Error(w, "bad id", http.StatusBadRequest)
		return
	}
	_ = exec.Command("tmux", "kill-session", "-t", "fc-"+id).Run()
	for _, ext := range []string{".cmd", ".sh", ".out", ".err", ".done"} {
		_ = os.Remove(tmpPrefix + id + ext)
	}
	w.WriteHeader(http.StatusNoContent)
}

// ── helpers ──────────────────────────────────────────────────────────────────

func reqTimeout(req execRequest) int {
	if req.TimeoutSecs <= 0 {
		return 60
	}
	return req.TimeoutSecs
}

func exitCode(err error, errBuf *bytes.Buffer) int {
	if err == nil {
		return 0
	}
	if ee, ok := err.(*exec.ExitError); ok {
		return ee.ExitCode()
	}
	errBuf.WriteString("\n" + err.Error())
	return -1
}

// readFrom returns the bytes of path from byte offset and the new end offset.
// A missing file (command not started writing yet) reads as empty at the same offset.
func readFrom(path string, off int) (string, int) {
	f, err := os.Open(path)
	if err != nil {
		return "", off
	}
	defer f.Close()
	if _, err := f.Seek(int64(off), 0); err != nil {
		return "", off
	}
	b, err := readAllCapped(f)
	if err != nil {
		return "", off
	}
	return string(b), off + len(b)
}

// readAllCapped reads up to 1 MiB per poll so a single response stays bounded;
// the host loops and advances the offset to drain the rest.
func readAllCapped(f *os.File) ([]byte, error) {
	const cap = 1 << 20
	buf := make([]byte, cap)
	n, err := f.Read(buf)
	if err != nil && n == 0 {
		return nil, nil // EOF or nothing new
	}
	return buf[:n], nil
}

func randID() (string, error) {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return hex.EncodeToString(b), nil
}

func validID(id string) bool {
	if len(id) != 32 {
		return false
	}
	for _, c := range id {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) {
			return false
		}
	}
	return true
}

func queryInt(r *http.Request, key string) int {
	if v, err := strconv.Atoi(r.URL.Query().Get(key)); err == nil && v >= 0 {
		return v
	}
	return 0
}

func shQuote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}
