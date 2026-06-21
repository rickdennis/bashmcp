package main

import "fmt"

// opResult is a per-item outcome of a bulk operation (destroy/drain/etc).
type opResult struct {
	VMID string
	Node string
	Err  error
}

// destroyAllVMs lists every VM on every node-agent and destroys it, never aborting on
// the first failure: each VM (and each unreachable/un-listable node) becomes one
// opResult so the caller can print a summary and set a non-zero exit code.
func destroyAllVMs(clients []*NodeClient) []opResult {
	var results []opResult
	for _, c := range clients {
		vms, err := c.ListVMs()
		if err != nil {
			results = append(results, opResult{Node: c.Node, Err: fmt.Errorf("list VMs: %w", err)})
			continue
		}
		for _, v := range vms {
			results = append(results, opResult{VMID: v.VMID, Node: c.Node, Err: c.Destroy(v.VMID)})
		}
	}
	return results
}
