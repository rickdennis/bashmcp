package main

import (
	"fmt"
	"sort"
	"sync"
	"time"
)

// clientsFromNodeAgents builds a NodeClient per node-agent that advertises a podIP.
func clientsFromNodeAgents(nas []NodeAgent, port int, timeout time.Duration) []*NodeClient {
	var clients []*NodeClient
	for _, n := range nas {
		if n.Spec.PodIP == "" {
			continue
		}
		clients = append(clients, newNodeClient(n.Spec.NodeName, n.Spec.PodIP, port, timeout))
	}
	return clients
}

// fanoutVMs asks every node-agent for its VMs concurrently. Unreachable agents are
// recorded in `reachable` (false) and contribute an empty slice rather than failing
// the whole gather — mirroring fc-top's degrade-don't-crash behavior.
func fanoutVMs(clients []*NodeClient) (vmsByNode map[string][]VM, reachable map[string]bool) {
	vmsByNode = make(map[string][]VM, len(clients))
	reachable = make(map[string]bool, len(clients))
	var mu sync.Mutex
	var wg sync.WaitGroup
	for _, c := range clients {
		wg.Add(1)
		go func(c *NodeClient) {
			defer wg.Done()
			vms, err := c.ListVMs()
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				reachable[c.Node] = false
				vmsByNode[c.Node] = nil
				return
			}
			reachable[c.Node] = true
			vmsByNode[c.Node] = vms
		}(c)
	}
	wg.Wait()
	return vmsByNode, reachable
}

// indexVMs maps each vm_id to the node that reported it.
func indexVMs(vmsByNode map[string][]VM) map[string]string {
	idx := make(map[string]string)
	for node, vms := range vmsByNode {
		for _, v := range vms {
			idx[v.VMID] = node
		}
	}
	return idx
}

// selectNodeForCreate picks the Ready node-agent with the most free taps (capacity),
// breaking ties by node name. Errors if no Ready node has capacity.
func selectNodeForCreate(nas []NodeAgent) (*NodeAgent, error) {
	viable := make([]NodeAgent, 0, len(nas))
	for _, n := range nas {
		if n.Status.Phase == "Ready" && n.Status.FreeTaps > 0 {
			viable = append(viable, n)
		}
	}
	if len(viable) == 0 {
		return nil, fmt.Errorf("no Ready node-agent has free capacity")
	}
	sort.Slice(viable, func(i, j int) bool {
		if viable[i].Status.FreeTaps != viable[j].Status.FreeTaps {
			return viable[i].Status.FreeTaps > viable[j].Status.FreeTaps
		}
		return viable[i].Spec.NodeName < viable[j].Spec.NodeName
	})
	pick := viable[0]
	return &pick, nil
}
