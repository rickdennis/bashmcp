package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/spf13/cobra"
)

// ─── agent-session data models ─────────────────────────────────────────────────

type AgentSession struct {
	ID          string         `json:"id"`
	AgentID     string         `json:"agent_id"`
	VMID        string         `json:"vm_id"`
	Status      string         `json:"status"`
	Title       string         `json:"title"`
	Resources   []SessionRes   `json:"resources"`
	EgressPolicy *EgressPolicy `json:"egress_policy"`
	CreatedAt   float64        `json:"created_at"`
	UpdatedAt   float64        `json:"updated_at"`
}

type SessionRes struct {
	Type          string `json:"type"`
	RepositoryURL string `json:"repository_url,omitempty"`
	Path          string `json:"path,omitempty"`
}

type EgressPolicy struct {
	Mode         string          `json:"mode"`
	AllowedHosts []string        `json:"allowed_hosts"`
	GitHub       *GitHubEgress   `json:"github,omitempty"`
}

type GitHubEgress struct {
	Repos []string `json:"repos"`
}

type SessionCreateReq struct {
	AgentID      string      `json:"agent_id"`
	Title        string      `json:"title,omitempty"`
	Resources    []map[string]any `json:"resources,omitempty"`
	EgressPolicy *EgressPolicy    `json:"egress_policy,omitempty"`
}

// ─── client helpers ────────────────────────────────────────────────────────────

func (c *NodeClient) CreateSession(req SessionCreateReq) (*AgentSession, error) {
	data, err := c.do(http.MethodPost, "/v1/sessions", req)
	if err != nil {
		return nil, err
	}
	var s AgentSession
	return &s, json.Unmarshal(data, &s)
}

func (c *NodeClient) ListSessions() ([]AgentSession, error) {
	data, err := c.do(http.MethodGet, "/v1/sessions", nil)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Data []AgentSession `json:"data"`
	}
	return resp.Data, json.Unmarshal(data, &resp)
}

func (c *NodeClient) SetEgressPolicy(sid string, pol EgressPolicy) error {
	_, err := c.do(http.MethodPost, "/v1/sessions/"+sid+"/egress-policy", pol)
	return err
}

// ─── commands ──────────────────────────────────────────────────────────────────

func sessionCmd() *cobra.Command {
	c := &cobra.Command{
		Use:   "session",
		Short: "Manage agent sessions (create, ls, egress)",
	}
	c.AddCommand(sessionCreateCmd(), sessionLsCmd(), sessionEgressCmd())
	return c
}

func sessionCreateCmd() *cobra.Command {
	var (
		agentID string
		title   string
		repos   []string
		hosts   []string
		node    string
	)
	c := &cobra.Command{
		Use:   "create",
		Short: "Create an agent session (boots a VM, optionally clones repos)",
		Example: `  # Secretless clone — broker injects the token:
  fcctl session create --agent myagent --repo rickdennis/bashmcp

  # Multiple repos + extra allowed hosts:
  fcctl session create --agent myagent --repo org/a --repo org/b --allow pypi.org

  # Target a specific node-agent:
  fcctl session create --agent myagent --repo org/repo --node fc-node-agent-0`,
		RunE: func(cmd *cobra.Command, args []string) error {
			if agentID == "" {
				return fmt.Errorf("--agent is required")
			}
			nc, err := nodeClientForSession(node)
			if err != nil {
				return err
			}

			req := SessionCreateReq{AgentID: agentID, Title: title}

			for _, r := range repos {
				req.Resources = append(req.Resources, map[string]any{
					"type":           "github_repository",
					"repository_url": r,
				})
			}

			if len(repos) > 0 || len(hosts) > 0 {
				pol := &EgressPolicy{
					Mode:         "default_deny",
					AllowedHosts: append([]string{"github.com", "*.githubusercontent.com"}, hosts...),
				}
				if len(repos) > 0 {
					slugs := make([]string, len(repos))
					for i, r := range repos {
						slugs[i] = repoSlug(r)
					}
					pol.GitHub = &GitHubEgress{Repos: slugs}
				}
				req.EgressPolicy = pol
			}

			fmt.Printf("Creating session (agent=%s", agentID)
			if len(repos) > 0 {
				fmt.Printf(", repos=%s", strings.Join(repos, ","))
			}
			fmt.Println(")…")

			sess, err := nc.CreateSession(req)
			if err != nil {
				return err
			}

			if isJSON() {
				return jsonOut(sess)
			}
			printSession(sess)
			return nil
		},
	}
	c.Flags().StringVar(&agentID, "agent", "", "agent ID (required)")
	c.Flags().StringVar(&title, "title", "", "session title")
	c.Flags().StringArrayVar(&repos, "repo", nil, "github repo to clone (owner/repo or https URL); repeatable")
	c.Flags().StringArrayVar(&hosts, "allow", nil, "extra allowed egress host; repeatable")
	c.Flags().StringVar(&node, "node", "", "target node-agent (e.g. fc-node-agent-0); default = router picks")
	return c
}

func sessionLsCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "ls",
		Short: "List agent sessions on a node",
		RunE: func(cmd *cobra.Command, args []string) error {
			nc, err := nodeClientForSession("")
			if err != nil {
				return err
			}
			sessions, err := nc.ListSessions()
			if err != nil {
				return err
			}
			if isJSON() {
				return jsonOut(sessions)
			}
			now := time.Now().UTC()
			fmt.Println(renderTable("Sessions", []string{"ID", "AGENT", "VM", "STATUS", "REPOS", "AGE"},
				agentSessionRows(now, sessions), 1))
			return nil
		},
	}
}

func sessionEgressCmd() *cobra.Command {
	var repos, hosts []string
	c := &cobra.Command{
		Use:   "egress <session-id>",
		Short: "Update the egress policy for a running session",
		Example: `  fcctl session egress sesn_abc123 --repo org/extra-repo --allow pypi.org`,
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			nc, err := nodeClientForSession("")
			if err != nil {
				return err
			}
			pol := EgressPolicy{
				Mode:         "default_deny",
				AllowedHosts: append([]string{"github.com", "*.githubusercontent.com"}, hosts...),
			}
			if len(repos) > 0 {
				slugs := make([]string, len(repos))
				for i, r := range repos {
					slugs[i] = repoSlug(r)
				}
				pol.GitHub = &GitHubEgress{Repos: slugs}
			}
			if err := nc.SetEgressPolicy(args[0], pol); err != nil {
				return err
			}
			fmt.Printf("egress policy updated for %s\n", args[0])
			return nil
		},
	}
	c.Flags().StringArrayVar(&repos, "repo", nil, "allowed github repo (owner/repo); repeatable")
	c.Flags().StringArrayVar(&hosts, "allow", nil, "extra allowed egress host; repeatable")
	return c
}

// ─── helpers ───────────────────────────────────────────────────────────────────

// nodeClientForSession returns a NodeClient. With --node it goes directly to that
// node-agent (matched by metadata.name = pod name); otherwise picks the first
// reachable Ready node.
func nodeClientForSession(podName string) (*NodeClient, error) {
	g := gather()
	if podName != "" {
		for _, na := range g.NodeAgents {
			if na.Meta.Name == podName {
				return newNodeClient(na.Meta.Name, na.Spec.PodIP, cfg.nodePort, 120*time.Second), nil
			}
		}
		return nil, fmt.Errorf("node-agent %q not found", podName)
	}
	// pick first ready+reachable node
	for _, na := range g.NodeAgents {
		if na.Status.Phase == "Ready" && g.Reachable[na.Spec.NodeName] {
			return newNodeClient(na.Meta.Name, na.Spec.PodIP, cfg.nodePort, 120*time.Second), nil
		}
	}
	return nil, fmt.Errorf("no reachable node-agent found")
}

func repoSlug(url string) string {
	s := strings.TrimSuffix(strings.TrimPrefix(url, "https://github.com/"), ".git")
	s = strings.TrimPrefix(s, "github.com/")
	return strings.Trim(s, "/")
}

func printSession(s *AgentSession) {
	age := "-"
	if s.CreatedAt > 0 {
		age = ageEpoch(time.Now().UTC(), s.CreatedAt)
	}
	repos := "-"
	if s.EgressPolicy != nil && s.EgressPolicy.GitHub != nil {
		repos = strings.Join(s.EgressPolicy.GitHub.Repos, ", ")
	}
	fmt.Printf("session: %s\n  agent:  %s\n  vm:     %s\n  status: %s\n  repos:  %s\n  age:    %s\n",
		s.ID, s.AgentID, s.VMID, s.Status, repos, age)
}

func agentSessionRows(now time.Time, sessions []AgentSession) [][]string {
	var rows [][]string
	for _, s := range sessions {
		repos := "-"
		if s.EgressPolicy != nil && s.EgressPolicy.GitHub != nil {
			repos = strings.Join(s.EgressPolicy.GitHub.Repos, ",")
		}
		age := "-"
		if s.CreatedAt > 0 {
			age = ageEpoch(now, s.CreatedAt)
		}
		id := s.ID
		if len(id) > 20 {
			id = id[:20]
		}
		rows = append(rows, []string{id, s.AgentID, s.VMID[:8], s.Status, repos, age})
	}
	return rows
}
