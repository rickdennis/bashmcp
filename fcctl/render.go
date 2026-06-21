package main

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/lipgloss/table"
)

// ─── colour palette (mirrors fc-top.py) ────────────────────────────────────────

var phaseStyle = map[string]lipgloss.Style{
	"Ready":       lipgloss.NewStyle().Foreground(lipgloss.Color("2")),
	"Bound":       lipgloss.NewStyle().Foreground(lipgloss.Color("2")),
	"Pending":     lipgloss.NewStyle().Foreground(lipgloss.Color("3")),
	"NotReady":    lipgloss.NewStyle().Foreground(lipgloss.Color("3")),
	"Lost":        lipgloss.NewStyle().Foreground(lipgloss.Color("1")),
	"Gone":        lipgloss.NewStyle().Foreground(lipgloss.Color("1")),
	"Terminating": lipgloss.NewStyle().Foreground(lipgloss.Color("8")),
}

var vmStyle = map[string]lipgloss.Style{
	"running":   lipgloss.NewStyle().Foreground(lipgloss.Color("2")),
	"paused":    lipgloss.NewStyle().Foreground(lipgloss.Color("6")),
	"creating":  lipgloss.NewStyle().Foreground(lipgloss.Color("3")),
	"booting":   lipgloss.NewStyle().Foreground(lipgloss.Color("3")),
	"error":     lipgloss.NewStyle().Foreground(lipgloss.Color("1")),
	"destroyed": lipgloss.NewStyle().Foreground(lipgloss.Color("8")),
}

var (
	headerStyle = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("13"))
	dimStyle    = lipgloss.NewStyle().Foreground(lipgloss.Color("8"))
	okStyle     = lipgloss.NewStyle().Foreground(lipgloss.Color("2"))
	downStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("1"))
)

// ─── pure helpers (unit-tested) ────────────────────────────────────────────────

func shortID(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

func humanDur(d time.Duration) string {
	s := int(d.Seconds())
	if s < 0 {
		s = 0
	}
	switch {
	case s < 60:
		return fmt.Sprintf("%ds", s)
	case s < 3600:
		return fmt.Sprintf("%dm%02ds", s/60, s%60)
	case s < 86400:
		return fmt.Sprintf("%dh%02dm", s/3600, (s%3600)/60)
	default:
		return fmt.Sprintf("%dd%02dh", s/86400, (s%86400)/3600)
	}
}

func ageEpoch(now time.Time, sec float64) string {
	if sec <= 0 {
		return "—"
	}
	return humanDur(now.Sub(time.Unix(int64(sec), 0)))
}

func ageISO(now time.Time, iso string) string {
	if iso == "" {
		return "—"
	}
	t, err := time.Parse(time.RFC3339, iso)
	if err != nil {
		return "—"
	}
	return humanDur(now.Sub(t))
}

func orDash(s string) string {
	if s == "" {
		return "—"
	}
	return s
}

func countByStatus(vmsByNode map[string][]VM, status string) int {
	n := 0
	for _, vms := range vmsByNode {
		for _, v := range vms {
			if v.Status == status {
				n++
			}
		}
	}
	return n
}

// nodeAgentRows: NODE PHASE FREE RUN PAUSE ERR HEARTBEAT REACH
func nodeAgentRows(now time.Time, nas []NodeAgent, vmsByNode map[string][]VM, reachable map[string]bool) [][]string {
	sorted := append([]NodeAgent(nil), nas...)
	sort.Slice(sorted, func(i, j int) bool { return sorted[i].Spec.NodeName < sorted[j].Spec.NodeName })
	rows := make([][]string, 0, len(sorted))
	for _, n := range sorted {
		node := n.Spec.NodeName
		var run, pause, errc int
		for _, v := range vmsByNode[node] {
			switch v.Status {
			case "running":
				run++
			case "paused":
				pause++
			case "error":
				errc++
			}
		}
		reach := "down"
		if reachable[node] {
			reach = "ok"
		}
		rows = append(rows, []string{
			node, n.Status.Phase, fmt.Sprintf("%d", n.Status.FreeTaps),
			fmt.Sprintf("%d", run), fmt.Sprintf("%d", pause), fmt.Sprintf("%d", errc),
			ageISO(now, n.Status.HeartbeatTime), reach,
		})
	}
	return rows
}

// sessionRows: SESSION NODE VM PHASE AGE
func sessionRows(now time.Time, sessions []Session) [][]string {
	sorted := append([]Session(nil), sessions...)
	sort.Slice(sorted, func(i, j int) bool {
		return sorted[i].Meta.CreationTimestamp < sorted[j].Meta.CreationTimestamp
	})
	rows := make([][]string, 0, len(sorted))
	for _, s := range sorted {
		rows = append(rows, []string{
			shortID(s.Spec.McpSessionID, 12), orDash(s.Spec.NodeName),
			orDash(shortID(s.Status.VMRef, 8)), orDash(s.Status.Phase),
			ageISO(now, s.Meta.CreationTimestamp),
		})
	}
	return rows
}

// vmRows: NODE VM NAME STATUS IP CPU MEM SNAP AGE
func vmRows(now time.Time, vmsByNode map[string][]VM) [][]string {
	type nv struct {
		node string
		v    VM
	}
	var all []nv
	for node, vms := range vmsByNode {
		for _, v := range vms {
			all = append(all, nv{node, v})
		}
	}
	sort.Slice(all, func(i, j int) bool {
		if all[i].node != all[j].node {
			return all[i].node < all[j].node
		}
		return all[i].v.CreatedAt < all[j].v.CreatedAt
	})
	rows := make([][]string, 0, len(all))
	for _, x := range all {
		snap := "·"
		if x.v.HasSnapshot {
			snap = "●"
		}
		rows = append(rows, []string{
			x.node, shortID(x.v.VMID, 8), orDash(x.v.Name), x.v.Status,
			orDash(x.v.IPAddress), fmt.Sprintf("%d", x.v.VCPU),
			fmt.Sprintf("%dM", x.v.MemMB), snap, ageEpoch(now, x.v.CreatedAt),
		})
	}
	return rows
}

func summaryLine(nas []NodeAgent, sessionCount int, vmsByNode map[string][]VM, leader string) string {
	ready, free, maxt := 0, 0, 0
	for _, n := range nas {
		if n.Status.Phase == "Ready" {
			ready++
		}
		free += n.Status.FreeTaps
		maxt += n.Spec.MaxVMs
	}
	return fmt.Sprintf("nodes %d/%d Ready   VMs %d▶ %d⏸ %d✖   free taps %d/%d   sessions %d   leader %s",
		ready, len(nas),
		countByStatus(vmsByNode, "running"), countByStatus(vmsByNode, "paused"), countByStatus(vmsByNode, "error"),
		free, maxt, sessionCount, orDash(leader))
}

// ─── lipgloss table rendering (visual glue) ────────────────────────────────────

// statusColor returns the style for a phase/vm-status/reach token, or empty.
func statusColor(tok string) lipgloss.Style {
	if st, ok := vmStyle[tok]; ok {
		return st
	}
	if st, ok := phaseStyle[tok]; ok {
		return st
	}
	switch tok {
	case "ok":
		return okStyle
	case "down":
		return downStyle
	}
	return lipgloss.NewStyle()
}

// renderTable styles a table; cells whose content is a known status/phase token are
// coloured. statusCols lists the column indices to colourize.
func renderTable(title string, headers []string, rows [][]string, statusCols ...int) string {
	colorset := map[int]bool{}
	for _, c := range statusCols {
		colorset[c] = true
	}
	t := table.New().
		Border(lipgloss.NormalBorder()).
		BorderStyle(dimStyle).
		Headers(headers...).
		Rows(rows...).
		StyleFunc(func(row, col int) lipgloss.Style {
			if row == table.HeaderRow {
				return headerStyle.Padding(0, 1)
			}
			base := lipgloss.NewStyle().Padding(0, 1)
			if colorset[col] && row >= 0 && row < len(rows) {
				return statusColor(rows[row][col]).Padding(0, 1)
			}
			return base
		})
	heading := headerStyle.Render(fmt.Sprintf("%s (%d)", title, len(rows)))
	return heading + "\n" + t.String()
}

// renderDashboard is the full ls/top view.
func renderDashboard(g Gather, now time.Time) string {
	var b strings.Builder
	b.WriteString(lipgloss.NewStyle().Foreground(lipgloss.Color("6")).Bold(true).Render("fc-mcp"))
	b.WriteString("  ")
	b.WriteString(summaryLine(g.NodeAgents, len(g.Sessions), g.VMsByNode, g.Leader))
	if g.KubeErr != "" {
		b.WriteString("\n")
		b.WriteString(downStyle.Render("kubectl: " + g.KubeErr))
	}
	b.WriteString("\n\n")
	b.WriteString(renderTable("NodeAgents", []string{"NODE", "PHASE", "FREE", "RUN", "PAUSE", "ERR", "HEARTBEAT", "REACH"},
		nodeAgentRows(now, g.NodeAgents, g.VMsByNode, g.Reachable), 1, 7))
	b.WriteString("\n")
	b.WriteString(renderTable("Sessions", []string{"SESSION", "NODE", "VM", "PHASE", "AGE"},
		sessionRows(now, g.Sessions), 3))
	b.WriteString("\n")
	b.WriteString(renderTable("VMs", []string{"NODE", "VM", "NAME", "STATUS", "IP", "CPU", "MEM", "SNAP", "AGE"},
		vmRows(now, g.VMsByNode), 3))
	return b.String()
}
