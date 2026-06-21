package main

import (
	"context"
	"fmt"
	"net/http"
	"testing"
)

type fakeAdapter struct {
	matchHost string
	injectErr error
	injected  bool
}

func (f *fakeAdapter) Name() string                                  { return "fake" }
func (f *fakeAdapter) Matches(host string, _ *EgressPolicy) bool      { return host == f.matchHost }
func (f *fakeAdapter) Inject(_ context.Context, _ *http.Request, _ *EgressPolicy) error {
	f.injected = true
	return f.injectErr
}

func req(t *testing.T, url string) *http.Request {
	t.Helper()
	r, err := http.NewRequest("GET", url, nil)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestRegistry_MatchingAdapterInjects(t *testing.T) {
	fa := &fakeAdapter{matchHost: "github.com"}
	reg := NewRegistry(fa)
	if err := reg.Apply(context.Background(), "github.com", req(t, "https://github.com/x/y/info/refs"), &EgressPolicy{}); err != nil {
		t.Fatalf("matching adapter should allow, got %v", err)
	}
	if !fa.injected {
		t.Errorf("the matching adapter's Inject should have run")
	}
}

func TestRegistry_InjectErrorDenies(t *testing.T) {
	fa := &fakeAdapter{matchHost: "github.com", injectErr: fmt.Errorf("repo not allowed")}
	reg := NewRegistry(fa)
	if err := reg.Apply(context.Background(), "github.com", req(t, "https://github.com/x/y/info/refs"), &EgressPolicy{}); err == nil {
		t.Errorf("an Inject error must deny the request")
	}
}

func TestRegistry_AllowlistPassthrough(t *testing.T) {
	reg := NewRegistry() // no adapters
	pol := &EgressPolicy{AllowedHosts: []string{"pypi.org"}}
	if err := reg.Apply(context.Background(), "pypi.org", req(t, "https://pypi.org/simple/"), pol); err != nil {
		t.Errorf("an allowlisted host should pass through, got %v", err)
	}
}

func TestRegistry_DefaultDeny(t *testing.T) {
	reg := NewRegistry()
	if err := reg.Apply(context.Background(), "evil.com", req(t, "https://evil.com/"), &EgressPolicy{}); err == nil {
		t.Errorf("an unmatched, non-allowlisted host must be denied")
	}
}

func TestRegistry_NilPolicyDenies(t *testing.T) {
	reg := NewRegistry()
	if err := reg.Apply(context.Background(), "github.com", req(t, "https://github.com/"), nil); err == nil {
		t.Errorf("a nil policy must default-deny")
	}
}
