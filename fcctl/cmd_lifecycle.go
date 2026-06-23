package main

import (
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

func createCmd() *cobra.Command {
	var node, name string
	var vcpu, memMB, diskMB int
	c := &cobra.Command{
		Use:   "create",
		Short: "Create a VM (node chosen by free capacity unless --node is given)",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			g := gather()
			targetNode := node
			if targetNode == "" {
				pick, err := selectNodeForCreate(g.NodeAgents)
				if err != nil {
					return err
				}
				targetNode = pick.Spec.NodeName
			}
			target := g.opClient(targetNode, createTimeout)
			if target == nil {
				return fmt.Errorf("node %q has no podIP / is unreachable", targetNode)
			}
			vm, err := target.Create(CreateReq{Name: name, VCPU: vcpu, MemMB: memMB, DiskMB: diskMB})
			if err != nil {
				return err
			}
			if isJSON() {
				return jsonOut(vm)
			}
			fmt.Printf("created %s (%s) on %s — %s\n", vm.VMID, orDash(vm.Name), target.Node, orDash(vm.IPAddress))
			return nil
		},
	}
	f := c.Flags()
	f.StringVar(&node, "node", "", "target node (default: most free capacity)")
	f.StringVar(&name, "name", "", "VM name (auto-generated if omitted)")
	f.IntVar(&vcpu, "vcpu", 0, "vCPUs (server default if 0)")
	f.IntVar(&memMB, "mem-mb", 0, "RAM in MB (server default if 0)")
	f.IntVar(&diskMB, "disk-mb", 0, "disk in MB (server default if 0)")
	return c
}

func execCmd() *cobra.Command {
	var timeout int
	var workdir string
	c := &cobra.Command{
		Use:   "exec <vm_id> [--timeout N] [--workdir D] -- <command...>",
		Short: "Run a command in a VM",
		Args:  cobra.MinimumNArgs(2),
		RunE: func(cmd *cobra.Command, args []string) error {
			dash := cmd.Flags().ArgsLenAtDash()
			if dash < 1 {
				return fmt.Errorf("usage: fcctl exec <vm_id> -- <command...>")
			}
			id := args[0]
			command := strings.Join(args[dash:], " ")
			if command == "" {
				return fmt.Errorf("empty command")
			}
			g := gather()
			node, fullID, ok := g.resolveVM(id)
			if !ok {
				return fmt.Errorf("no such VM %s on any node", id)
			}
			// The HTTP ceiling must exceed the in-VM --timeout, or a long command
			// dies at the transport layer ("context deadline exceeded") first.
			client := g.opClient(node, execClientTimeout(timeout))
			if client == nil {
				return fmt.Errorf("owning node %s for %s is unreachable", node, id)
			}
			res, err := client.Exec(fullID, command, workdir, timeout)
			if err != nil {
				return err
			}
			if isJSON() {
				return jsonOut(res)
			}
			fmt.Print(res.Stdout)
			if res.Stderr != "" {
				fmt.Fprint(os.Stderr, res.Stderr)
			}
			if res.ReturnCode != 0 {
				os.Exit(res.ReturnCode & 0xff)
			}
			return nil
		},
	}
	f := c.Flags()
	f.IntVar(&timeout, "timeout", 60, "max seconds")
	f.StringVar(&workdir, "workdir", "", "working directory inside the VM")
	return c
}

// resolveOne is the shared "find the owning node then act" helper. The timeout is
// the HTTP ceiling for the operation's client — set per-op so a slow pause/resume/
// destroy isn't cut off by the short discovery timeout.
// resolveOne resolves a vm_id (prefix or full) to a node client and calls action
// with the FULL vm_id. This ensures the node-agent receives the complete UUID even
// when the user typed the 8-char display prefix from `fcctl top`.
func resolveOne(id string, timeout time.Duration, action func(*NodeClient, string) error) error {
	g := gather()
	node, fullID, ok := g.resolveVM(id)
	if !ok {
		return fmt.Errorf("no such VM %s on any node", id)
	}
	client := g.opClient(node, timeout)
	if client == nil {
		return fmt.Errorf("owning node %s for %s is unreachable", node, id)
	}
	return action(client, fullID)
}

func pauseCmd() *cobra.Command {
	return &cobra.Command{
		Use: "pause <vm_id>", Short: "Snapshot + pause a VM", Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return resolveOne(args[0], pauseTimeout, func(c *NodeClient, id string) error {
				if err := c.Pause(id); err != nil {
					return err
				}
				fmt.Printf("paused %s on %s\n", id, c.Node)
				return nil
			})
		},
	}
}

func resumeCmd() *cobra.Command {
	return &cobra.Command{
		Use: "resume <vm_id>", Short: "Resume a VM from its snapshot", Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return resolveOne(args[0], resumeTimeout, func(c *NodeClient, id string) error {
				if err := c.Resume(id); err != nil {
					return err
				}
				fmt.Printf("resumed %s on %s\n", id, c.Node)
				return nil
			})
		},
	}
}

func destroyCmd() *cobra.Command {
	return &cobra.Command{
		Use: "destroy <vm_id>", Short: "Destroy a VM and delete all its data", Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return resolveOne(args[0], destroyTimeout, func(c *NodeClient, id string) error {
				if !confirm(fmt.Sprintf("About to destroy VM %s on %s.", id, c.Node)) {
					return fmt.Errorf("aborted")
				}
				if err := c.Destroy(id); err != nil {
					return err
				}
				fmt.Printf("destroyed %s\n", id)
				return nil
			})
		},
	}
}

func restoreCmd() *cobra.Command {
	var node, session string
	c := &cobra.Command{
		Use: "restore <vm_id> --node N", Short: "Restore a VM from S3 onto a node", Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			if node == "" {
				return fmt.Errorf("--node is required for restore")
			}
			g := gather()
			target := g.opClient(node, restoreTimeout)
			if target == nil {
				return fmt.Errorf("no node-agent %q", node)
			}
			if err := target.Restore(args[0], session); err != nil {
				return err
			}
			fmt.Printf("restored %s onto %s\n", args[0], node)
			return nil
		},
	}
	c.Flags().StringVar(&node, "node", "", "node to restore onto (required)")
	c.Flags().StringVar(&session, "session", "", "session id to bind to the restored VM")
	return c
}
