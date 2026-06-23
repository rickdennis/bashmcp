package main

import (
	"fmt"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

func lsCmd() *cobra.Command {
	c := &cobra.Command{
		Use:       "ls {vms|sessions|nodes}",
		Short:     "List VMs, sessions, or node-agents",
		Args:      cobra.ExactArgs(1),
		ValidArgs: []string{"vms", "sessions", "nodes"},
		RunE: func(cmd *cobra.Command, args []string) error {
			now := time.Now().UTC()
			switch args[0] {
			case "vms":
				g := gather()
				if isJSON() {
					return jsonOut(flattenVMs(g.VMsByNode))
				}
				fmt.Println(renderTable("VMs", []string{"NODE", "VM", "NAME", "STATUS", "IP", "CPU", "MEM", "SNAP", "AGE"},
					vmRows(now, g.VMsByNode), 3))
				return nil
			case "sessions":
				ss, err := kube.Sessions()
				if err != nil {
					return err
				}
				if isJSON() {
					return jsonOut(ss)
				}
				fmt.Println(renderTable("MCP Sessions (router)", []string{"SESSION", "NODE", "VM", "PHASE", "AGE"},
					sessionRows(now, ss), 3))
				return nil
			case "nodes":
				g := gather()
				if isJSON() {
					return jsonOut(g.NodeAgents)
				}
				if g.KubeErr != "" {
					fmt.Println(downStyle.Render("kubectl: " + g.KubeErr))
				}
				fmt.Println(renderTable("NodeAgents", []string{"NODE", "PHASE", "FREE", "RUN", "PAUSE", "ERR", "HEARTBEAT", "REACH"},
					nodeAgentRows(now, g.NodeAgents, g.VMsByNode, g.Reachable), 1, 7))
				return nil
			default:
				return fmt.Errorf("unknown resource %q (want vms|sessions|nodes)", args[0])
			}
		},
	}
	return c
}

func flattenVMs(vmsByNode map[string][]VM) []VM {
	var out []VM
	for _, vms := range vmsByNode {
		out = append(out, vms...)
	}
	return out
}

func getCmd() *cobra.Command {
	c := &cobra.Command{
		Use:       "get {vm|session|node} <id>",
		Short:     "Show full detail for one VM, session, or node-agent",
		Args:      cobra.ExactArgs(2),
		ValidArgs: []string{"vm", "session", "node"},
		RunE: func(cmd *cobra.Command, args []string) error {
			kind, id := args[0], args[1]
			now := time.Now().UTC()
			switch kind {
			case "vm":
				return getVM(id)
			case "session":
				return getSession(id, now)
			case "node":
				return getNode(id, now)
			default:
				return fmt.Errorf("unknown kind %q (want vm|session|node)", kind)
			}
		},
	}
	return c
}

func getVM(id string) error {
	g := gather()
	node, ok := g.resolveNode(id)
	if !ok {
		return fmt.Errorf("no such VM %s on any node", id)
	}
	client := g.clientFor(node)
	if client == nil {
		return fmt.Errorf("owning node %s for %s is unreachable", node, id)
	}
	d, err := client.GetVM(id)
	if err != nil {
		return err
	}
	if isJSON() {
		return jsonOut(d)
	}
	fmt.Printf("vm_id      %s\n", d.VMID)
	fmt.Printf("name       %s\n", d.Name)
	fmt.Printf("node       %s\n", d.Node)
	fmt.Printf("status     %s\n", statusColor(d.Status).Render(d.Status))
	fmt.Printf("ip         %s\n", orDash(d.IPAddress))
	fmt.Printf("vcpu/mem   %d vCPU / %d MB\n", d.VCPU, d.MemMB)
	fmt.Printf("disk       %d MB\n", d.DiskMB)
	fmt.Printf("pid        %s\n", pidStr(d.PID))
	if d.Snapshot != nil {
		fmt.Printf("snapshot   %.1f MB @ %s\n", d.Snapshot.MemSizeMB, ageEpoch(time.Now().UTC(), d.Snapshot.CreatedAt)+" ago")
	} else {
		fmt.Printf("snapshot   (none)\n")
	}
	if d.Error != "" {
		fmt.Printf("error      %s\n", downStyle.Render(d.Error))
	}
	return nil
}

func pidStr(pid int) string {
	if pid == 0 {
		return "—"
	}
	return fmt.Sprintf("%d", pid)
}

func getSession(id string, now time.Time) error {
	ss, err := kube.Sessions()
	if err != nil {
		return err
	}
	want := strings.TrimPrefix(id, "s-")
	for _, s := range ss {
		if s.Spec.McpSessionID == want || s.Meta.Name == id || s.Meta.Name == "s-"+want {
			if isJSON() {
				return jsonOut(s)
			}
			fmt.Printf("session    %s\n", s.Spec.McpSessionID)
			fmt.Printf("cr name    %s\n", s.Meta.Name)
			fmt.Printf("node       %s\n", orDash(s.Spec.NodeName))
			fmt.Printf("phase      %s\n", statusColor(s.Status.Phase).Render(orDash(s.Status.Phase)))
			fmt.Printf("vm         %s\n", orDash(s.Status.VMRef))
			fmt.Printf("age        %s\n", ageISO(now, s.Meta.CreationTimestamp))
			return nil
		}
	}
	return fmt.Errorf("no session %q", id)
}

func getNode(name string, now time.Time) error {
	g := gather()
	for _, n := range g.NodeAgents {
		if n.Spec.NodeName != name {
			continue
		}
		if isJSON() {
			return jsonOut(n)
		}
		reach := "down"
		if g.Reachable[name] {
			reach = "ok"
		}
		fmt.Printf("node       %s\n", n.Spec.NodeName)
		fmt.Printf("phase      %s\n", statusColor(n.Status.Phase).Render(orDash(n.Status.Phase)))
		fmt.Printf("capacity   %d free / %d max taps\n", n.Status.FreeTaps, n.Spec.MaxVMs)
		fmt.Printf("podIP      %s  (%s)\n", n.Spec.PodIP, statusColor(reach).Render(reach))
		fmt.Printf("heartbeat  %s ago\n", ageISO(now, n.Status.HeartbeatTime))
		fmt.Println()
		fmt.Println(renderTable("VMs on "+name, []string{"NODE", "VM", "NAME", "STATUS", "IP", "CPU", "MEM", "SNAP", "AGE"},
			vmRows(now, map[string][]VM{name: g.VMsByNode[name]}), 3))
		return nil
	}
	return fmt.Errorf("no node-agent %q", name)
}
