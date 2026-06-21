package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestOctoSts_ExchangesAndParses(t *testing.T) {
	exp := time.Now().Add(time.Hour).UTC().Truncate(time.Second)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer oidc-xyz" {
			t.Errorf("Authorization = %q, want Bearer oidc-xyz", got)
		}
		if got := r.URL.Query().Get("scope"); got != "acme/widgets" {
			t.Errorf("scope = %q, want acme/widgets", got)
		}
		fmt.Fprintf(w, `{"token":"ghs_abc","expires_at":%q}`, exp.Format(time.RFC3339))
	}))
	defer srv.Close()

	src := newOctoStsTokenSource(srv.URL, "", func(_ context.Context, session string) (string, error) {
		if session != "sesn_x" {
			t.Errorf("mint called with session %q, want sesn_x", session)
		}
		return "oidc-xyz", nil
	})

	tok, gotExp, err := src.Token(context.Background(), "sesn_x", "acme", "widgets")
	if err != nil {
		t.Fatalf("Token: %v", err)
	}
	if tok != "ghs_abc" {
		t.Errorf("token = %q, want ghs_abc", tok)
	}
	if !gotExp.Equal(exp) {
		t.Errorf("exp = %v, want %v", gotExp, exp)
	}
}

func TestOctoSts_OIDCMintErrorPropagates(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("octo-sts must not be called when OIDC minting fails")
	}))
	defer srv.Close()

	src := newOctoStsTokenSource(srv.URL, "", func(_ context.Context, _ string) (string, error) {
		return "", fmt.Errorf("kms unavailable")
	})
	if _, _, err := src.Token(context.Background(), "sesn_x", "acme", "widgets"); err == nil {
		t.Errorf("an OIDC mint error must propagate")
	}
}

func TestOctoSts_NonSuccessIsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "no matching trust policy", http.StatusForbidden)
	}))
	defer srv.Close()

	src := newOctoStsTokenSource(srv.URL, "", func(_ context.Context, _ string) (string, error) {
		return "oidc-xyz", nil
	})
	if _, _, err := src.Token(context.Background(), "sesn_x", "acme", "widgets"); err == nil {
		t.Errorf("a non-2xx response must be an error")
	}
}

func TestOctoSts_EmptyTokenIsError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprint(w, `{"token":""}`)
	}))
	defer srv.Close()

	src := newOctoStsTokenSource(srv.URL, "", func(_ context.Context, _ string) (string, error) {
		return "oidc-xyz", nil
	})
	if _, _, err := src.Token(context.Background(), "sesn_x", "acme", "widgets"); err == nil {
		t.Errorf("an empty token in a 200 response must be an error")
	}
}
