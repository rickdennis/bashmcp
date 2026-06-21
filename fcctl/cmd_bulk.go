package main

import (
	"fmt"

	"github.com/spf13/cobra"
)

func drainCmd() *cobra.Command {
	return &cobra.Command{
		Use: "drain <node>", Short: "Pause+snapshot every running VM on a node", Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			node := args[0]
			g := gather()
			c := g.opClient(node, drainTimeout)
			if c == nil {
				return fmt.Errorf("no node-agent %q", node)
			}
			running := 0
			for _, v := range g.VMsByNode[node] {
				if v.Status == "running" {
					running++
				}
			}
			if !confirm(fmt.Sprintf("About to pause+snapshot %d running VM(s) on %s.", running, node)) {
				return fmt.Errorf("aborted")
			}
			out, err := c.Drain()
			if err != nil {
				return err
			}
			fmt.Printf("drained %s: %s\n", node, string(out))
			return nil
		},
	}
}

func resetCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "reset",
		Short: "Destroy ALL VMs on ALL nodes, then delete ALL Session CRs",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			g := gather()
			total := 0
			for _, vms := range g.VMsByNode {
				total += len(vms)
			}
			ss, _ := kube.Sessions()
			if !confirm(fmt.Sprintf("About to destroy %d VM(s) across %d node(s) and delete %d Session CR(s).",
				total, len(g.Clients), len(ss))) {
				return fmt.Errorf("aborted")
			}

			results := destroyAllVMs(g.opClients(destroyTimeout))
			ok, failed := 0, 0
			for _, r := range results {
				if r.Err != nil {
					failed++
					if r.VMID != "" {
						fmt.Printf("  DELETE %s (%s) FAILED: %v\n", shortID(r.VMID, 8), r.Node, r.Err)
					} else {
						fmt.Printf("  node %s FAILED: %v\n", r.Node, r.Err)
					}
				} else {
					ok++
				}
			}
			fmt.Printf("destroyed %d VM(s), %d failure(s)\n", ok, failed)

			msg, err := kube.DeleteAllSessions()
			if err != nil {
				fmt.Printf("session delete FAILED: %v\n", err)
				failed++
			} else {
				fmt.Printf("sessions: %s\n", msg)
			}

			// Re-verify empty.
			g2 := gather()
			remaining := 0
			for _, vms := range g2.VMsByNode {
				remaining += len(vms)
			}
			fmt.Printf("verify: %d VM(s) remaining\n", remaining)
			if failed > 0 || remaining > 0 {
				return fmt.Errorf("reset incomplete: %d failure(s), %d VM(s) remaining", failed, remaining)
			}
			return nil
		},
	}
}
