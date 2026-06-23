package main

import (
	"testing"
	"time"
)

func TestShortID(t *testing.T) {
	if got := shortID("abcdef1234567890", 8); got != "abcdef12" {
		t.Errorf("shortID = %q", got)
	}
	if got := shortID("", 8); got != "" {
		t.Errorf("shortID empty = %q", got)
	}
}

func TestAgeEpoch(t *testing.T) {
	now := time.Date(2026, 6, 18, 12, 0, 0, 0, time.UTC)
	cases := []struct {
		secsAgo float64
		want    string
	}{
		{0, "0s"},
		{45, "45s"},
		{90, "1m30s"},
		{3661, "1h01m"},
		{90061, "1d01h"},
	}
	for _, c := range cases {
		ts := float64(now.Unix()) - c.secsAgo
		if got := ageEpoch(now, ts); got != c.want {
			t.Errorf("ageEpoch(%.0fs ago) = %q, want %q", c.secsAgo, got, c.want)
		}
	}
	if got := ageEpoch(now, 0); got != "—" {
		t.Errorf("ageEpoch(0) = %q, want em-dash", got)
	}
}

func TestAgeISO(t *testing.T) {
	now := time.Date(2026, 6, 18, 12, 0, 0, 0, time.UTC)
	if got := ageISO(now, "2026-06-18T11:58:30Z"); got != "1m30s" {
		t.Errorf("ageISO = %q, want 1m30s", got)
	}
	if got := ageISO(now, ""); got != "—" {
		t.Errorf("ageISO empty = %q", got)
	}
	if got := ageISO(now, "garbage"); got != "—" {
		t.Errorf("ageISO garbage = %q", got)
	}
}

func TestVMRows(t *testing.T) {
	now := time.Date(2026, 6, 18, 12, 0, 0, 0, time.UTC)
	vmsByNode := map[string][]VM{
		"worker-1": {{
			VMID: "vm-abcdef123456", Name: "alpha", Status: "running",
			IPAddress: "172.16.0.2", VCPU: 1, MemMB: 512, HasSnapshot: false,
			CreatedAt: float64(now.Unix()) - 90,
		}},
	}
	rows := vmRows(now, vmsByNode)
	if len(rows) != 1 {
		t.Fatalf("want 1 row, got %d", len(rows))
	}
	r := rows[0]
	want := []string{"worker-1", "vm-abcde", "alpha", "running", "172.16.0.2", "1", "512M", "·", "1m30s"}
	for i := range want {
		if r[i] != want[i] {
			t.Errorf("col %d = %q, want %q (row=%v)", i, r[i], want[i], r)
		}
	}
}

func TestSummaryLine(t *testing.T) {
	nas := []NodeAgent{nodeAgent("w1", "Ready", 30), nodeAgent("w2", "Ready", 28)}
	vmsByNode := map[string][]VM{
		"w1": {{Status: "running"}, {Status: "paused"}},
		"w2": {{Status: "error"}},
	}
	s := summaryLine(nas, 1, vmsByNode, "router-x")
	// Spot-check the salient counts appear, format-agnostic.
	for _, sub := range []string{"2/2", "1", "router-x", "58"} { // free taps 30+28=58
		if !contains(s, sub) {
			t.Errorf("summary %q missing %q", s, sub)
		}
	}
}

func contains(s, sub string) bool {
	return len(sub) == 0 || (len(s) >= len(sub) && indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
