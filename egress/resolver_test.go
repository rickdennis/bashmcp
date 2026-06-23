package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func writeIndex(t *testing.T, path string, m map[string]*EgressPolicy) {
	t.Helper()
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, b, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestFileIndexResolver_LoadAndLookup(t *testing.T) {
	path := filepath.Join(t.TempDir(), "egress-index.json")
	pol := &EgressPolicy{
		Mode:         "default_deny",
		AllowedHosts: []string{"github.com"},
		GitHub:       &GitHubPolicy{Repos: []string{"acme/widgets"}},
	}
	writeIndex(t, path, map[string]*EgressPolicy{"172.16.0.7": pol})

	r := newFileIndexResolver(path)
	got := r.Resolve("172.16.0.7")
	if got == nil || !got.GitHub.RepoAllowed("acme", "widgets") {
		t.Fatalf("Resolve(known) = %+v, want a policy allowing acme/widgets", got)
	}
	if r.Resolve("172.16.0.99") != nil {
		t.Errorf("Resolve(unknown) should be nil (default-deny)")
	}
}

func TestFileIndexResolver_MissingFile(t *testing.T) {
	r := newFileIndexResolver(filepath.Join(t.TempDir(), "nope.json"))
	if r.Resolve("172.16.0.7") != nil {
		t.Errorf("missing index should resolve nil (default-deny), not panic")
	}
}

func TestFileIndexResolver_ReloadOnChange(t *testing.T) {
	path := filepath.Join(t.TempDir(), "egress-index.json")
	writeIndex(t, path, map[string]*EgressPolicy{"172.16.0.7": {AllowedHosts: []string{"github.com"}}})
	r := newFileIndexResolver(path)

	writeIndex(t, path, map[string]*EgressPolicy{"172.16.0.8": {AllowedHosts: []string{"pypi.org"}}})
	future := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(path, future, future); err != nil {
		t.Fatal(err)
	}
	if err := r.reloadIfChanged(); err != nil {
		t.Fatal(err)
	}
	if r.Resolve("172.16.0.8") == nil {
		t.Errorf("after reload, 172.16.0.8 should resolve")
	}
	if r.Resolve("172.16.0.7") != nil {
		t.Errorf("after reload, the old 172.16.0.7 entry should be gone")
	}
}

func TestFileIndexResolver_KeepsLastGoodOnParseError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "egress-index.json")
	writeIndex(t, path, map[string]*EgressPolicy{"172.16.0.7": {AllowedHosts: []string{"github.com"}}})
	r := newFileIndexResolver(path)

	if err := os.WriteFile(path, []byte("{ not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	future := time.Now().Add(2 * time.Second)
	if err := os.Chtimes(path, future, future); err != nil {
		t.Fatal(err)
	}
	if err := r.reloadIfChanged(); err == nil {
		t.Errorf("reloadIfChanged should return an error on malformed JSON")
	}
	if r.Resolve("172.16.0.7") == nil {
		t.Errorf("on parse error the last good index must be preserved")
	}
}
