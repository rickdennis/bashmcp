package main

import "testing"

func TestLoadConfig_Defaults(t *testing.T) {
	c := loadConfig(nil)
	if c.ListenAddr != "127.0.0.1:3129" {
		t.Errorf("ListenAddr default = %q, want 127.0.0.1:3129", c.ListenAddr)
	}
	if c.GitHubBackend != "octosts" {
		t.Errorf("GitHubBackend default = %q, want octosts", c.GitHubBackend)
	}
	if c.Fwmark != 1 {
		t.Errorf("Fwmark default = %d, want 1", c.Fwmark)
	}
}

func TestLoadConfig_Overrides(t *testing.T) {
	c := loadConfig([]string{"-listen", "0.0.0.0:9999", "-github-backend", "appmint", "-app-id", "55"})
	if c.ListenAddr != "0.0.0.0:9999" {
		t.Errorf("listen override = %q", c.ListenAddr)
	}
	if c.GitHubBackend != "appmint" {
		t.Errorf("backend override = %q", c.GitHubBackend)
	}
	if c.AppID != 55 {
		t.Errorf("app-id override = %d", c.AppID)
	}
}
