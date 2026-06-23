package main

import (
	"context"
	"fmt"
	"net/http"
)

// adapter.go — the credential-injection plug-in layer and the default-deny gate.
//
// A CredentialAdapter handles a class of destination (GitHub, later Snowflake/MCP): it
// decides whether it owns a host and, if so, mutates the decrypted request to add the
// right credential. The Registry is the egress decision point sitting between the MITM
// layer and re-origination.

type CredentialAdapter interface {
	// Name identifies the adapter (for logging/metrics).
	Name() string
	// Matches reports whether this adapter owns requests to host under this policy.
	Matches(host string, pol *EgressPolicy) bool
	// Inject mutates req to add the destination's credential. Returning an error denies
	// the request (it is never forwarded).
	Inject(ctx context.Context, req *http.Request, pol *EgressPolicy) error
}

// Registry holds the ordered adapters and applies the egress decision.
type Registry struct {
	adapters []CredentialAdapter
}

func NewRegistry(adapters ...CredentialAdapter) *Registry {
	return &Registry{adapters: adapters}
}

// Apply enforces the egress decision for a request to host: the first adapter that
// Matches runs its Inject (whose error denies); otherwise an allowlisted host passes
// through unmodified; otherwise the request is denied. nil return = proceed (possibly
// mutated); non-nil = deny.
func (reg *Registry) Apply(ctx context.Context, host string, req *http.Request, pol *EgressPolicy) error {
	for _, a := range reg.adapters {
		if a.Matches(host, pol) {
			return a.Inject(ctx, req, pol)
		}
	}
	if pol.HostAllowed(host) {
		return nil
	}
	return fmt.Errorf("egress denied: %s is not permitted by the session policy", host)
}
