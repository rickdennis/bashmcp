package main

import (
	"context"
	"encoding/base64"
	"testing"
	"time"
)

type fakeTokenSource struct {
	token                         string
	err                           error
	gotSession, gotOwner, gotRepo string
	called                        bool
}

func (f *fakeTokenSource) Token(_ context.Context, session, owner, repo string) (string, time.Time, error) {
	f.called = true
	f.gotSession, f.gotOwner, f.gotRepo = session, owner, repo
	return f.token, time.Now().Add(time.Hour), f.err
}

func TestGitHubAdapter_Matches(t *testing.T) {
	a := NewGitHubAdapter(&fakeTokenSource{})
	for host, want := range map[string]bool{
		"github.com":     true,
		"GitHub.com":     true,
		"api.github.com": false,
		"evil.com":       false,
	} {
		if got := a.Matches(host, nil); got != want {
			t.Errorf("Matches(%q) = %v, want %v", host, got, want)
		}
	}
}

func TestGitHubAdapter_InjectsAllowedRepo(t *testing.T) {
	fts := &fakeTokenSource{token: "tkn123"}
	a := NewGitHubAdapter(fts)
	pol := &EgressPolicy{SessionID: "sesn_x", GitHub: &GitHubPolicy{Repos: []string{"acme/widgets"}}}
	r := req(t, "https://github.com/acme/widgets.git/info/refs?service=git-upload-pack")

	if err := a.Inject(context.Background(), r, pol); err != nil {
		t.Fatalf("allowed repo should inject, got %v", err)
	}
	want := "Basic " + base64.StdEncoding.EncodeToString([]byte("x-access-token:tkn123"))
	if got := r.Header.Get("Authorization"); got != want {
		t.Errorf("Authorization = %q, want %q", got, want)
	}
	if fts.gotSession != "sesn_x" || fts.gotOwner != "acme" || fts.gotRepo != "widgets" {
		t.Errorf("token requested for (%q,%q,%q), want (sesn_x,acme,widgets)", fts.gotSession, fts.gotOwner, fts.gotRepo)
	}
}

func TestGitHubAdapter_DeniesDisallowedRepo(t *testing.T) {
	fts := &fakeTokenSource{token: "tkn"}
	a := NewGitHubAdapter(fts)
	pol := &EgressPolicy{GitHub: &GitHubPolicy{Repos: []string{"acme/widgets"}}}
	r := req(t, "https://github.com/acme/secret/info/refs")

	if err := a.Inject(context.Background(), r, pol); err == nil {
		t.Errorf("a repo not in policy must be denied")
	}
	if r.Header.Get("Authorization") != "" {
		t.Errorf("no Authorization should be set on a denied request")
	}
	if fts.called {
		t.Errorf("the token source must not be called for a disallowed repo")
	}
}

func TestGitHubAdapter_DeniesNonGitPath(t *testing.T) {
	a := NewGitHubAdapter(&fakeTokenSource{token: "tkn"})
	pol := &EgressPolicy{GitHub: &GitHubPolicy{Repos: []string{"acme/widgets"}}}
	r := req(t, "https://github.com/acme/widgets") // web page, not a git op

	if err := a.Inject(context.Background(), r, pol); err == nil {
		t.Errorf("a non-git path must be denied (can't be repo-scoped)")
	}
}

func TestGitHubAdapter_DeniesNilPolicy(t *testing.T) {
	// A nil policy (unknown IP / default-deny) must be denied cleanly, not panic, even
	// though Matches owns github.com regardless of policy.
	a := NewGitHubAdapter(&fakeTokenSource{token: "tkn"})
	r := req(t, "https://github.com/acme/widgets/info/refs")
	if err := a.Inject(context.Background(), r, nil); err == nil {
		t.Errorf("a nil policy must be denied")
	}
}

func TestGitHubAdapter_DeniesOnTokenError(t *testing.T) {
	fts := &fakeTokenSource{err: context.DeadlineExceeded}
	a := NewGitHubAdapter(fts)
	pol := &EgressPolicy{GitHub: &GitHubPolicy{Repos: []string{"acme/widgets"}}}
	r := req(t, "https://github.com/acme/widgets/info/refs")

	if err := a.Inject(context.Background(), r, pol); err == nil {
		t.Errorf("a token-source error must deny the request")
	}
	if r.Header.Get("Authorization") != "" {
		t.Errorf("no Authorization should be set when token minting fails")
	}
}
