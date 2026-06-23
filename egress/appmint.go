package main

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// appmint.go — the drop-in alternative TokenSource: the broker holds a GitHub App private
// key and mints installation tokens directly (App JWT -> installation lookup -> scoped
// access token with repositories:[repo]). Selected when octo-sts isn't usable. The key is
// a host secret; it is never exposed to the VM. JWT signing is pure stdlib (no jwt lib).

type appMintTokenSource struct {
	appID      int64
	privateKey *rsa.PrivateKey
	apiBase    string // https://api.github.com; overridable for tests
	client     *http.Client
	now        func() time.Time
}

func newAppMintTokenSource(appID int64, key *rsa.PrivateKey, apiBase string) *appMintTokenSource {
	if apiBase == "" {
		apiBase = "https://api.github.com"
	}
	return &appMintTokenSource{appID: appID, privateKey: key, apiBase: apiBase, client: http.DefaultClient, now: time.Now}
}

// signAppJWT builds a short-lived RS256 GitHub App JWT (iss=appID).
func signAppJWT(appID int64, key *rsa.PrivateKey, now time.Time) (string, error) {
	b64 := func(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }
	header := b64([]byte(`{"alg":"RS256","typ":"JWT"}`))
	claims, err := json.Marshal(map[string]any{
		"iat": now.Add(-time.Minute).Unix(), // clock-skew slack
		"exp": now.Add(9 * time.Minute).Unix(),
		"iss": appID,
	})
	if err != nil {
		return "", err
	}
	signingInput := header + "." + b64(claims)
	sum := sha256.Sum256([]byte(signingInput))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, sum[:])
	if err != nil {
		return "", err
	}
	return signingInput + "." + b64(sig), nil
}

func (a *appMintTokenSource) Token(ctx context.Context, sessionID, owner, repo string) (string, time.Time, error) {
	jwt, err := signAppJWT(a.appID, a.privateKey, a.now())
	if err != nil {
		return "", time.Time{}, fmt.Errorf("signing app JWT: %w", err)
	}
	var inst struct {
		ID int64 `json:"id"`
	}
	if err := a.apiDo(ctx, jwt, http.MethodGet, fmt.Sprintf("/repos/%s/%s/installation", owner, repo), nil, &inst); err != nil {
		return "", time.Time{}, fmt.Errorf("looking up installation for %s/%s: %w", owner, repo, err)
	}
	reqBody := map[string]any{
		"repositories": []string{repo},
		"permissions":  map[string]string{"contents": "read"},
	}
	var out struct {
		Token     string `json:"token"`
		ExpiresAt string `json:"expires_at"`
	}
	if err := a.apiDo(ctx, jwt, http.MethodPost, fmt.Sprintf("/app/installations/%d/access_tokens", inst.ID), reqBody, &out); err != nil {
		return "", time.Time{}, fmt.Errorf("creating installation token: %w", err)
	}
	if out.Token == "" {
		return "", time.Time{}, fmt.Errorf("github returned an empty installation token")
	}
	exp := a.now().Add(50 * time.Minute)
	if out.ExpiresAt != "" {
		if p, err := time.Parse(time.RFC3339, out.ExpiresAt); err == nil {
			exp = p
		}
	}
	return out.Token, exp, nil
}

// apiDo issues an authenticated GitHub API call, JSON-encoding body (if non-nil) and
// decoding a 2xx response into out; non-2xx is an error.
func (a *appMintTokenSource) apiDo(ctx context.Context, jwt, method, path string, body any, out any) error {
	var rdr io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rdr = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, a.apiBase+path, rdr)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+jwt)
	req.Header.Set("Accept", "application/vnd.github+json")
	if rdr != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := a.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode/100 != 2 {
		return fmt.Errorf("%s %s: %s: %s", method, path, resp.Status, strings.TrimSpace(string(b)))
	}
	return json.Unmarshal(b, out)
}
