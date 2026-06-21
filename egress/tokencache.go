package main

import (
	"context"
	"sync"
	"time"
)

// tokencache.go — a caching decorator over any TokenSource. It memoizes tokens per
// (session, owner/repo), refreshes them a configurable skew before expiry, and coalesces
// concurrent requests for the same key (per-key lock) so a burst of git requests for one
// repo triggers a single upstream mint. Errors are never cached.

type cachingTokenSource struct {
	inner   TokenSource
	skew    time.Duration    // refresh when less than this remains before expiry
	now     func() time.Time // injectable clock (tests)
	mu      sync.Mutex
	entries map[string]*tokenCacheEntry
}

type tokenCacheEntry struct {
	mu    sync.Mutex
	token string
	exp   time.Time
}

func newCachingTokenSource(inner TokenSource, skew time.Duration) *cachingTokenSource {
	return &cachingTokenSource{
		inner:   inner,
		skew:    skew,
		now:     time.Now,
		entries: map[string]*tokenCacheEntry{},
	}
}

func (c *cachingTokenSource) Token(ctx context.Context, sessionID, owner, repo string) (string, time.Time, error) {
	key := sessionID + "|" + owner + "/" + repo

	c.mu.Lock()
	e, ok := c.entries[key]
	if !ok {
		e = &tokenCacheEntry{}
		c.entries[key] = e
	}
	c.mu.Unlock()

	// Per-key lock coalesces concurrent mints for the same repo without blocking others.
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.token != "" && c.now().Before(e.exp.Add(-c.skew)) {
		return e.token, e.exp, nil
	}
	tok, exp, err := c.inner.Token(ctx, sessionID, owner, repo)
	if err != nil {
		return "", time.Time{}, err
	}
	e.token, e.exp = tok, exp
	return tok, exp, nil
}
