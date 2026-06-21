package main

import "testing"

func TestHostAllowed(t *testing.T) {
	p := &EgressPolicy{AllowedHosts: []string{"github.com", "*.githubusercontent.com"}}
	cases := []struct {
		host string
		want bool
	}{
		{"github.com", true},
		{"GitHub.com", true},                       // case-insensitive
		{"api.github.com", false},                  // exact entry doesn't match subdomain
		{"codeload.githubusercontent.com", true},   // matches *.githubusercontent.com
		{"githubusercontent.com", false},           // apex doesn't match the wildcard
		{"evil.com", false},
	}
	for _, c := range cases {
		if got := p.HostAllowed(c.host); got != c.want {
			t.Errorf("HostAllowed(%q) = %v, want %v", c.host, got, c.want)
		}
	}
	var nilP *EgressPolicy
	if nilP.HostAllowed("github.com") {
		t.Errorf("nil policy HostAllowed should be false (default-deny)")
	}
}

func TestRepoAllowed(t *testing.T) {
	g := &GitHubPolicy{Repos: []string{"acme/widgets", "myorg/*"}}
	cases := []struct {
		owner, repo string
		want        bool
	}{
		{"acme", "widgets", true},   // exact
		{"Acme", "Widgets", true},   // case-insensitive
		{"acme", "other", false},    // exact entry only
		{"myorg", "anything", true}, // myorg/* wildcard
		{"notlisted", "x", false},
	}
	for _, c := range cases {
		if got := g.RepoAllowed(c.owner, c.repo); got != c.want {
			t.Errorf("RepoAllowed(%q,%q) = %v, want %v", c.owner, c.repo, got, c.want)
		}
	}
	var nilG *GitHubPolicy
	if nilG.RepoAllowed("acme", "widgets") {
		t.Errorf("nil GitHubPolicy RepoAllowed should be false")
	}
}
