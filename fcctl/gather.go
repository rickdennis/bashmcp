package main

import "time"

// Gather is the full cluster snapshot used by `ls` and `top`.
type Gather struct {
	NodeAgents []NodeAgent
	Sessions   []Session
	Leader     string
	VMsByNode  map[string][]VM
	Reachable  map[string]bool
	KubeErr    string // non-empty if the kubectl reads degraded (shown in-band, not fatal)
	Clients    []*NodeClient
	Port       int // node-agent REST port (so op clients can be rebuilt at other timeouts)
}

// gatherAll reads the CRs via kubectl and fans out /vms across node-agents. Like
// fc-top, kubectl failures degrade (recorded in KubeErr) rather than aborting. The
// `discover` timeout is the SHORT per-node ceiling used only for the /vms fan-out so
// an unreachable node fails fast and renders `down`; long-running operations
// (exec/create/pause/...) build their own clients via opClient with a higher ceiling.
func gatherAll(k *Kube, lease string, port int, discover time.Duration) Gather {
	var g Gather
	g.Port = port
	nas, err := k.NodeAgents()
	if err != nil {
		g.KubeErr = err.Error()
	}
	g.NodeAgents = nas
	if ss, err := k.Sessions(); err == nil {
		g.Sessions = ss
	}
	if holder, err := k.LeaseHolder(lease); err == nil {
		g.Leader = holder
	}
	g.Clients = clientsFromNodeAgents(nas, port, discover)
	g.VMsByNode, g.Reachable = fanoutVMs(g.Clients)
	return g
}

// clientFor returns the SHORT-timeout discovery NodeClient for a node (used by reads).
func (g Gather) clientFor(node string) *NodeClient {
	for _, c := range g.Clients {
		if c.Node == node {
			return c
		}
	}
	return nil
}

// opClient builds a fresh client to a node's podIP with the given timeout — for
// long-running operations that must NOT inherit the short discovery ceiling (the
// "context deadline exceeded" exec bug). Returns nil if the node has no known podIP.
func (g Gather) opClient(node string, timeout time.Duration) *NodeClient {
	for _, n := range g.NodeAgents {
		if n.Spec.NodeName == node && n.Spec.PodIP != "" {
			return newNodeClient(node, n.Spec.PodIP, g.Port, timeout)
		}
	}
	return nil
}

// opClients builds op clients (given timeout) for every node-agent with a podIP.
func (g Gather) opClients(timeout time.Duration) []*NodeClient {
	return clientsFromNodeAgents(g.NodeAgents, g.Port, timeout)
}

// resolveNode finds which node owns a vm_id (by the fanned /vms index).
func (g Gather) resolveNode(vmID string) (string, bool) {
	node, ok := indexVMs(g.VMsByNode)[vmID]
	return node, ok
}
