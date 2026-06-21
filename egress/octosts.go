package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// octosts.go — the default TokenSource. It mints a session-identity OIDC token and
// exchanges it at an octo-sts instance for a short-lived, repo-scoped GitHub App
// installation token. The GitHub App private key lives in octo-sts, never here.
//
// NOTE: the exact octo-sts wire contract (endpoint shape, param/field names) must be
// confirmed against the target instance — that's the plan's flagged open item. It is
// isolated to this file so a mismatch is a localized change. We use: POST with the OIDC
// token as a Bearer, scope=owner/repo (+ optional identity/policy name), and a JSON
// response {token, expires_at}.

// oidcMintFunc produces an OIDC token asserting the session's identity (the subject the
// octo-sts trust policy matches). In standalone v1 the broker self-signs this.
type oidcMintFunc func(ctx context.Context, sessionID string) (string, error)

type octoStsTokenSource struct {
	endpoint string
	policy   string // octo-sts trust-policy ("identity") name; optional
	mintOIDC oidcMintFunc
	client   *http.Client
}

func newOctoStsTokenSource(endpoint, policy string, mint oidcMintFunc) *octoStsTokenSource {
	return &octoStsTokenSource{endpoint: endpoint, policy: policy, mintOIDC: mint, client: http.DefaultClient}
}

type octoStsResponse struct {
	Token     string `json:"token"`
	ExpiresAt string `json:"expires_at"`
}

func (o *octoStsTokenSource) Token(ctx context.Context, sessionID, owner, repo string) (string, time.Time, error) {
	oidc, err := o.mintOIDC(ctx, sessionID)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("minting session OIDC token: %w", err)
	}
	u, err := url.Parse(o.endpoint)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("invalid octo-sts endpoint: %w", err)
	}
	q := u.Query()
	q.Set("scope", owner+"/"+repo)
	if o.policy != "" {
		q.Set("identity", o.policy)
	}
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u.String(), nil)
	if err != nil {
		return "", time.Time{}, err
	}
	req.Header.Set("Authorization", "Bearer "+oidc)
	req.Header.Set("Accept", "application/json")

	resp, err := o.client.Do(req)
	if err != nil {
		return "", time.Time{}, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode/100 != 2 {
		return "", time.Time{}, fmt.Errorf("octo-sts exchange %s: %s", resp.Status, strings.TrimSpace(string(body)))
	}
	var r octoStsResponse
	if err := json.Unmarshal(body, &r); err != nil {
		return "", time.Time{}, fmt.Errorf("parsing octo-sts response: %w", err)
	}
	if r.Token == "" {
		return "", time.Time{}, fmt.Errorf("octo-sts returned an empty token")
	}
	exp := time.Now().Add(50 * time.Minute) // installation tokens last ~1h; default if unstated
	if r.ExpiresAt != "" {
		if parsed, err := time.Parse(time.RFC3339, r.ExpiresAt); err == nil {
			exp = parsed
		}
	}
	return r.Token, exp, nil
}
