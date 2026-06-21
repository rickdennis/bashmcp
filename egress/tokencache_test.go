package main

import (
	"context"
	"sync"
	"testing"
	"time"
)

type countingSource struct {
	mu    sync.Mutex
	calls int
	token string
	exp   time.Time
	err   error
}

func (s *countingSource) Token(_ context.Context, _, _, _ string) (string, time.Time, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.calls++
	return s.token, s.exp, s.err
}

func (s *countingSource) count() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.calls
}

func TestTokenCache_CachesPerKey(t *testing.T) {
	src := &countingSource{token: "tok", exp: time.Now().Add(time.Hour)}
	c := newCachingTokenSource(src, time.Minute)
	for i := 0; i < 3; i++ {
		tok, _, err := c.Token(context.Background(), "sesn", "acme", "widgets")
		if err != nil || tok != "tok" {
			t.Fatalf("Token = (%q,%v), want (tok,nil)", tok, err)
		}
	}
	if src.count() != 1 {
		t.Errorf("inner called %d times, want 1 (cached)", src.count())
	}
}

func TestTokenCache_DistinctKeys(t *testing.T) {
	src := &countingSource{token: "tok", exp: time.Now().Add(time.Hour)}
	c := newCachingTokenSource(src, time.Minute)
	c.Token(context.Background(), "sesn", "acme", "widgets")
	c.Token(context.Background(), "sesn", "acme", "other")  // different repo
	c.Token(context.Background(), "other", "acme", "widgets") // different session
	if src.count() != 3 {
		t.Errorf("inner called %d times, want 3 (distinct keys not coalesced)", src.count())
	}
}

func TestTokenCache_RefreshesNearExpiry(t *testing.T) {
	base := time.Now()
	src := &countingSource{token: "tok", exp: base.Add(10 * time.Minute)}
	c := newCachingTokenSource(src, 2*time.Minute) // refresh when <2m remain (i.e. at base+8m)
	clock := base
	c.now = func() time.Time { return clock }

	c.Token(context.Background(), "sesn", "acme", "widgets") // mint #1
	clock = base.Add(7 * time.Minute)                        // 3m left > skew -> cached
	c.Token(context.Background(), "sesn", "acme", "widgets")
	if src.count() != 1 {
		t.Fatalf("at t+7m it should still be cached, calls=%d want 1", src.count())
	}
	clock = base.Add(9 * time.Minute) // 1m left < skew -> refresh
	c.Token(context.Background(), "sesn", "acme", "widgets")
	if src.count() != 2 {
		t.Errorf("at t+9m it should refresh, calls=%d want 2", src.count())
	}
}

func TestTokenCache_ErrorNotCached(t *testing.T) {
	src := &countingSource{err: context.DeadlineExceeded}
	c := newCachingTokenSource(src, time.Minute)
	if _, _, err := c.Token(context.Background(), "sesn", "acme", "widgets"); err == nil {
		t.Fatal("expected the inner error to propagate")
	}
	src.err, src.token, src.exp = nil, "tok", time.Now().Add(time.Hour)
	if _, _, err := c.Token(context.Background(), "sesn", "acme", "widgets"); err != nil {
		t.Fatalf("retry after error should succeed, got %v", err)
	}
	if src.count() != 2 {
		t.Errorf("inner called %d times, want 2 (an error must not be cached)", src.count())
	}
}
