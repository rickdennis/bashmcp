package main

import (
	"flag"
	"time"
)

// config.go — fc-egress runtime configuration (flags/env). All paths default to the
// shared-PV layout under /opt/fc-mcp so the sidecar and node-agent agree.

type Config struct {
	ListenAddr     string        // TPROXY listener (must match the nft `tproxy ... to` target)
	HealthAddr     string        // liveness endpoint
	IndexPath      string        // ip->policy index written by the node-agent (shared PV)
	CACertPath     string        // egress CA cert PEM
	CAKeyPath      string        // egress CA key PEM (host-only)
	GenerateCAOnly bool          // generate the CA if missing, then exit (init step)
	Fwmark         int           // SO_MARK on re-originated sockets; must match the nft rule
	PollInterval   time.Duration // egress-index reload poll
	TokenSkew      time.Duration // refresh tokens this long before expiry
	GitHubBackend  string        // "octosts" | "appmint"
	OctoStsURL     string
	OctoStsPolicy  string
	AppID          int64
	AppKeyPath     string
}

func loadConfig(args []string) *Config {
	c := &Config{}
	fs := flag.NewFlagSet("fc-egress", flag.ContinueOnError)
	fs.StringVar(&c.ListenAddr, "listen", "127.0.0.1:3129", "TPROXY listener address")
	fs.StringVar(&c.HealthAddr, "health", "127.0.0.1:3130", "health endpoint address")
	fs.StringVar(&c.IndexPath, "index", "/opt/fc-mcp/egress-index.json", "ip->policy index (shared PV)")
	fs.StringVar(&c.CACertPath, "ca-cert", "/opt/fc-mcp/egress-ca/ca.crt", "egress CA cert PEM")
	fs.StringVar(&c.CAKeyPath, "ca-key", "/opt/fc-mcp/egress-ca/ca.key", "egress CA key PEM")
	fs.BoolVar(&c.GenerateCAOnly, "generate-ca", false, "generate the CA if missing, then exit")
	fs.IntVar(&c.Fwmark, "fwmark", 1, "SO_MARK on re-originated sockets (must match the nft rule)")
	fs.DurationVar(&c.PollInterval, "poll", time.Second, "egress-index reload poll interval")
	fs.DurationVar(&c.TokenSkew, "token-skew", 2*time.Minute, "refresh tokens this long before expiry")
	fs.StringVar(&c.GitHubBackend, "github-backend", "octosts", "octosts | appmint")
	fs.StringVar(&c.OctoStsURL, "octosts-url", "", "octo-sts exchange endpoint")
	fs.StringVar(&c.OctoStsPolicy, "octosts-policy", "", "octo-sts trust-policy (identity) name")
	fs.Int64Var(&c.AppID, "app-id", 0, "GitHub App ID (appmint backend)")
	fs.StringVar(&c.AppKeyPath, "app-key", "", "GitHub App private key PEM file (appmint backend)")
	_ = fs.Parse(args)
	return c
}
