package main

import (
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/spf13/cobra"
)

func topCmd() *cobra.Command {
	c := &cobra.Command{
		Use:   "top",
		Short: "Live dashboard (refreshes on a ticker; q to quit, r to refresh now)",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			if cfg.refresh < 1 {
				cfg.refresh = 2
			}
			m := topModel{interval: time.Duration(cfg.refresh) * time.Second}
			_, err := tea.NewProgram(m, tea.WithAltScreen()).Run()
			return err
		},
	}
	c.Flags().IntVar(&cfg.refresh, "refresh", 2, "refresh interval seconds")
	return c
}

// gatherMsg carries a fresh gather back to the UI goroutine.
type gatherMsg struct {
	g  Gather
	at time.Time
}

type topModel struct {
	interval time.Duration
	last     Gather
	at       time.Time
	ready    bool
}

func (m topModel) Init() tea.Cmd {
	return tea.Batch(doGather(), tick(m.interval))
}

// doGather runs the (slow) kubectl + HTTP gather OFF the UI goroutine and delivers
// the result as a message — never inside Update/View.
func doGather() tea.Cmd {
	return func() tea.Msg {
		return gatherMsg{g: gather(), at: time.Now().UTC()}
	}
}

func tick(d time.Duration) tea.Cmd {
	return tea.Tick(d, func(t time.Time) tea.Msg { return tickMsg{} })
}

type tickMsg struct{}

func (m topModel) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.KeyMsg:
		switch msg.String() {
		case "q", "ctrl-c", "ctrl+c", "esc":
			return m, tea.Quit
		case "r":
			return m, doGather()
		}
	case gatherMsg:
		m.last = msg.g
		m.at = msg.at
		m.ready = true
		return m, nil
	case tickMsg:
		return m, tea.Batch(doGather(), tick(m.interval))
	}
	return m, nil
}

func (m topModel) View() string {
	if !m.ready {
		return "gathering…  (q to quit)\n"
	}
	return renderDashboard(m.last, m.at) + "\n\n" + dimStyle.Render("q quit · r refresh · "+m.at.Format("15:04:05 UTC")) + "\n"
}
