package main

import "context"

// dataplane.go — the L4/L7 interception boundary, and the ONLY Envoy-replaceable component.
//
// Everything the data plane depends on — PolicyResolver, Registry, CredentialAdapter,
// certCache, the TokenSources — is transport-agnostic. Swapping the stdlib TPROXY data
// plane (tproxy.go) for Envoy means: PolicyResolver becomes an ext_authz gRPC service,
// CredentialAdapter.Inject becomes an ext_proc header mutation, and certCache moves into
// Envoy's SDS. None of those types change. This interface is where that fork would happen.
type DataPlane interface {
	Serve(ctx context.Context) error
}
