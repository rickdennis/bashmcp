package main

import "testing"

func TestParseGitRepo(t *testing.T) {
	cases := []struct {
		path      string
		owner     string
		repo      string
		ok        bool
	}{
		// ref advertisement (the first request of a clone/fetch)
		{"/acme/widgets/info/refs", "acme", "widgets", true},
		{"/acme/widgets.git/info/refs", "acme", "widgets", true},
		// upload-pack (clone/fetch data)
		{"/acme/widgets/git-upload-pack", "acme", "widgets", true},
		{"/acme/widgets.git/git-upload-pack", "acme", "widgets", true},
		// receive-pack (push)
		{"/acme/widgets/git-receive-pack", "acme", "widgets", true},
		// hyphens/dots in names are legal
		{"/my-org/my.repo.git/info/refs", "my-org", "my.repo", true},

		// not git operations
		{"/acme/widgets", "", "", false},         // bare web path, no service suffix
		{"/acme", "", "", false},                 // owner only
		{"/", "", "", false},                     // root
		{"", "", "", false},                      // empty
		{"/acme/widgets/tree/main", "", "", false}, // web tree view
		{"/acme/widgets/extra/info/refs", "", "", false}, // deeper than owner/repo
	}
	for _, c := range cases {
		owner, repo, ok := parseGitRepo(c.path)
		if owner != c.owner || repo != c.repo || ok != c.ok {
			t.Errorf("parseGitRepo(%q) = (%q,%q,%v), want (%q,%q,%v)",
				c.path, owner, repo, ok, c.owner, c.repo, c.ok)
		}
	}
}
