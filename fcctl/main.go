package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

type config struct {
	namespace  string
	nodePort   int
	lease      string
	output     string
	yes        bool
	kubeconfig string
	refresh    int
	timeout    time.Duration
}

var (
	cfg  = &config{}
	kube *Kube
)

func main() {
	if err := rootCmd().Execute(); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func rootCmd() *cobra.Command {
	root := &cobra.Command{
		Use:           "fcctl",
		Short:         "Admin CLI for the fc-mcp HA stack (VMs, sessions, node-agents)",
		SilenceUsage:  true,
		SilenceErrors: true,
		PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
			kc, err := resolveKubeconfig(cfg.kubeconfig)
			if err != nil {
				return err
			}
			bin := os.Getenv("KUBECTL")
			if bin == "" {
				bin = "kubectl"
			}
			kube = &Kube{Bin: bin, Kubeconfig: kc, Namespace: cfg.namespace}
			cfg.timeout = discoverTimeout // short /vms fan-out ceiling; ops use their own
			return nil
		},
	}
	pf := root.PersistentFlags()
	pf.StringVar(&cfg.namespace, "namespace", envOr("FC_MCP_NAMESPACE", "fc-mcp"), "Kubernetes namespace")
	pf.IntVar(&cfg.nodePort, "node-port", envOrInt("FC_MCP_NODE_PORT", 8080), "node-agent REST port")
	pf.StringVar(&cfg.lease, "lease", envOr("FC_MCP_LEASE", "fc-mcp-router-leader"), "router leader lease name")
	pf.StringVarP(&cfg.output, "output", "o", "table", "output format: table|json")
	pf.BoolVarP(&cfg.yes, "yes", "y", false, "skip confirmation on destructive commands")
	pf.StringVar(&cfg.kubeconfig, "kubeconfig", "", "path to kubeconfig (default $KUBECONFIG or ~/.kube/config)")

	root.AddCommand(
		lsCmd(), getCmd(), createCmd(), execCmd(),
		pauseCmd(), resumeCmd(), restoreCmd(), destroyCmd(),
		drainCmd(), resetCmd(), topCmd(),
	)
	return root
}

// ─── shared helpers ────────────────────────────────────────────────────────────

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envOrInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

func jsonOut(v any) error {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	fmt.Println(string(b))
	return nil
}

func isJSON() bool { return strings.EqualFold(cfg.output, "json") }

func gather() Gather {
	return gatherAll(kube, cfg.lease, cfg.nodePort, cfg.timeout)
}

// confirm prompts unless -y was passed. Returns true to proceed.
func confirm(prompt string) bool {
	if cfg.yes {
		return true
	}
	fmt.Printf("%s [y/N] ", prompt)
	r := bufio.NewReader(os.Stdin)
	line, _ := r.ReadString('\n')
	line = strings.TrimSpace(strings.ToLower(line))
	return line == "y" || line == "yes"
}
