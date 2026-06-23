package main

import (
	"encoding/json"
	"os"
	"strings"
	"sync"
	"time"
)

// policy.go — the egress policy model and matching, plus the PolicyResolver seam.
//
// EgressPolicy is the per-session enforcement record. The node-agent denormalizes one
// per VM IP into egress-index.json on the shared PV; the proxy loads that into memory
// (see fileIndexResolver) and consults it per connection. A nil policy = default-deny.

// GitHubPolicy lists the repos a session may reach, as "owner/repo" or "owner/*".
type GitHubPolicy struct {
	Repos []string `json:"repos"`
}

// EgressPolicy is the per-session egress decision input.
type EgressPolicy struct {
	SessionID    string        `json:"session_id"`    // owning session (token scoping + audit)
	Mode         string        `json:"mode"`          // "default_deny" (the only v1 mode)
	AllowedHosts []string      `json:"allowed_hosts"` // SNI passthrough allowlist (exact or "*.suffix")
	GitHub       *GitHubPolicy `json:"github,omitempty"`
}

// HostAllowed reports whether host matches the passthrough allowlist. Entries are either
// an exact host or a "*.suffix" wildcard (matching any host with >=1 label before suffix).
// A nil policy denies everything (default-deny).
func (p *EgressPolicy) HostAllowed(host string) bool {
	if p == nil {
		return false
	}
	host = strings.ToLower(strings.TrimSuffix(host, "."))
	for _, entry := range p.AllowedHosts {
		entry = strings.ToLower(entry)
		if suffix, ok := strings.CutPrefix(entry, "*."); ok {
			if strings.HasSuffix(host, "."+suffix) {
				return true
			}
		} else if host == entry {
			return true
		}
	}
	return false
}

// RepoAllowed reports whether owner/repo matches the policy's repo list. Entries are an
// exact "owner/repo" or an "owner/*" wildcard. A nil policy denies everything.
func (g *GitHubPolicy) RepoAllowed(owner, repo string) bool {
	if g == nil {
		return false
	}
	full := strings.ToLower(owner + "/" + repo)
	ownerWild := strings.ToLower(owner) + "/*"
	for _, entry := range g.Repos {
		entry = strings.ToLower(entry)
		if entry == full || entry == ownerWild {
			return true
		}
	}
	return false
}

// PolicyResolver maps a connection's guest source IP to its session's egress policy.
// A nil return means default-deny. This is the seam an Envoy ext_authz would implement.
type PolicyResolver interface {
	Resolve(srcIP string) *EgressPolicy
}

// fileIndexResolver reads the node-agent-maintained egress-index.json ({ip: policy}) on
// the shared PV into an in-memory map, reloading only when the file's mtime changes. All
// per-connection lookups are O(1) RAM reads — no kube or localhost calls on the hot path.
type fileIndexResolver struct {
	path  string
	mu    sync.RWMutex
	index map[string]*EgressPolicy
	mtime time.Time
}

func newFileIndexResolver(path string) *fileIndexResolver {
	r := &fileIndexResolver{path: path, index: map[string]*EgressPolicy{}}
	_ = r.reloadIfChanged()
	return r
}

func (r *fileIndexResolver) Resolve(srcIP string) *EgressPolicy {
	r.mu.RLock()
	defer r.mu.RUnlock()
	return r.index[srcIP]
}

// reloadIfChanged reloads the index when the file's mtime differs from the last load.
// A missing file leaves the index empty (default-deny everywhere); a malformed file
// returns an error and preserves the last good index (atomic writes upstream make a
// partial read unlikely, but we never fail open or wipe the map on a parse error).
func (r *fileIndexResolver) reloadIfChanged() error {
	fi, err := os.Stat(r.path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	r.mu.RLock()
	unchanged := fi.ModTime().Equal(r.mtime)
	r.mu.RUnlock()
	if unchanged {
		return nil
	}
	b, err := os.ReadFile(r.path)
	if err != nil {
		return err
	}
	var idx map[string]*EgressPolicy
	if err := json.Unmarshal(b, &idx); err != nil {
		return err
	}
	r.mu.Lock()
	r.index, r.mtime = idx, fi.ModTime()
	r.mu.Unlock()
	return nil
}
