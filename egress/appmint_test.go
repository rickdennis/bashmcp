package main

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func testKey(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	k, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	return k
}

func TestSignAppJWT(t *testing.T) {
	key := testKey(t)
	tok, err := signAppJWT(1234, key, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	parts := strings.Split(tok, ".")
	if len(parts) != 3 {
		t.Fatalf("JWT should have 3 dot-separated parts, got %d", len(parts))
	}

	var hdr map[string]any
	hb, _ := base64.RawURLEncoding.DecodeString(parts[0])
	if err := json.Unmarshal(hb, &hdr); err != nil {
		t.Fatal(err)
	}
	if hdr["alg"] != "RS256" {
		t.Errorf("alg = %v, want RS256", hdr["alg"])
	}

	var claims map[string]any
	cb, _ := base64.RawURLEncoding.DecodeString(parts[1])
	if err := json.Unmarshal(cb, &claims); err != nil {
		t.Fatal(err)
	}
	if fmt.Sprint(claims["iss"]) != "1234" {
		t.Errorf("iss = %v, want 1234", claims["iss"])
	}

	signing := parts[0] + "." + parts[1]
	sig, _ := base64.RawURLEncoding.DecodeString(parts[2])
	h := sha256.Sum256([]byte(signing))
	if err := rsa.VerifyPKCS1v15(&key.PublicKey, crypto.SHA256, h[:], sig); err != nil {
		t.Errorf("signature should verify with the public key: %v", err)
	}
}

func TestAppMint_MintsScopedToken(t *testing.T) {
	key := testKey(t)
	exp := time.Now().Add(time.Hour).UTC().Truncate(time.Second)
	mux := http.NewServeMux()
	mux.HandleFunc("/repos/acme/widgets/installation", func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.Header.Get("Authorization"), "Bearer ") {
			t.Errorf("installation lookup missing Bearer JWT")
		}
		fmt.Fprint(w, `{"id":42}`)
	})
	mux.HandleFunc("/app/installations/42/access_tokens", func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Repositories []string `json:"repositories"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		if len(body.Repositories) != 1 || body.Repositories[0] != "widgets" {
			t.Errorf("repositories = %v, want [widgets]", body.Repositories)
		}
		fmt.Fprintf(w, `{"token":"ghs_scoped","expires_at":%q}`, exp.Format(time.RFC3339))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()

	src := newAppMintTokenSource(1234, key, srv.URL)
	tok, gotExp, err := src.Token(context.Background(), "sesn", "acme", "widgets")
	if err != nil {
		t.Fatalf("Token: %v", err)
	}
	if tok != "ghs_scoped" {
		t.Errorf("token = %q, want ghs_scoped", tok)
	}
	if !gotExp.Equal(exp) {
		t.Errorf("exp = %v, want %v", gotExp, exp)
	}
}

func TestAppMint_InstallationLookupError(t *testing.T) {
	key := testKey(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "not found", http.StatusNotFound)
	}))
	defer srv.Close()
	src := newAppMintTokenSource(1234, key, srv.URL)
	if _, _, err := src.Token(context.Background(), "sesn", "acme", "widgets"); err == nil {
		t.Errorf("an installation-lookup failure must be an error")
	}
}
