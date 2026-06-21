package main

import (
	"context"
	"encoding/base64"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// github.go — the GitHub CredentialAdapter: recognizes git-over-HTTPS smart-protocol
// requests, extracts owner/repo from the path, checks it against the session's egress
// policy, and injects a short-lived repo-scoped token via the configured TokenSource.

// TokenSource yields a short-lived GitHub token scoped to a single repo for a session.
// The default implementation exchanges a session-identity OIDC token at octo-sts; an
// app-mint implementation mints installation tokens directly. The VM never sees it.
type TokenSource interface {
	Token(ctx context.Context, sessionID, owner, repo string) (token string, exp time.Time, err error)
}

// GitHubAdapter injects repo-scoped credentials into git-over-HTTPS requests to github.com.
type GitHubAdapter struct {
	src TokenSource
}

func NewGitHubAdapter(src TokenSource) *GitHubAdapter { return &GitHubAdapter{src: src} }

func (a *GitHubAdapter) Name() string { return "github" }

// Matches owns git-over-HTTPS traffic to github.com.
func (a *GitHubAdapter) Matches(host string, _ *EgressPolicy) bool {
	return strings.EqualFold(host, "github.com")
}

// Inject parses owner/repo from the git smart-HTTP path, denies if the path isn't a git
// op or the repo isn't in policy, then sets the Basic auth header from a freshly minted
// repo-scoped token. Any failure denies the request (returns an error).
func (a *GitHubAdapter) Inject(ctx context.Context, req *http.Request, pol *EgressPolicy) error {
	if pol == nil {
		return fmt.Errorf("no egress policy for this session (default-deny)")
	}
	owner, repo, ok := parseGitRepo(req.URL.Path)
	if !ok {
		return fmt.Errorf("not a git operation: %s (cannot be repo-scoped)", req.URL.Path)
	}
	if !pol.GitHub.RepoAllowed(owner, repo) {
		return fmt.Errorf("repo %s/%s not permitted by session policy", owner, repo)
	}
	token, _, err := a.src.Token(ctx, pol.SessionID, owner, repo)
	if err != nil {
		return fmt.Errorf("minting token for %s/%s: %w", owner, repo, err)
	}
	cred := base64.StdEncoding.EncodeToString([]byte("x-access-token:" + token))
	req.Header.Set("Authorization", "Basic "+cred)
	return nil
}

// gitOpSuffixes are the path tails of the git smart-HTTP protocol that carry a real
// repo operation (everything else on github.com is a web page or REST call).
var gitOpSuffixes = []string{"info/refs", "git-upload-pack", "git-receive-pack"}

// parseGitRepo extracts (owner, repo) from a git smart-HTTP request path and reports
// whether the path is a recognized git operation. A trailing ".git" on the repo
// segment is stripped. Non-git paths (web pages, API calls, malformed or deeper paths)
// return ok=false.
func parseGitRepo(path string) (owner, repo string, ok bool) {
	parts := strings.SplitN(strings.TrimPrefix(path, "/"), "/", 3)
	if len(parts) < 3 {
		return "", "", false
	}
	owner = parts[0]
	repo = strings.TrimSuffix(parts[1], ".git")
	if owner == "" || repo == "" {
		return "", "", false
	}
	for _, s := range gitOpSuffixes {
		if parts[2] == s {
			return owner, repo, true
		}
	}
	return "", "", false
}
