package main

import "time"

// Two distinct ceilings. The DISCOVER timeout is the short per-node ceiling for the
// /vms fan-out behind `ls`/`top` — an unreachable node should fail fast and render
// `down`, not stall the whole view. Long-running OPERATIONS get their own, generous
// HTTP ceilings (built via Gather.opClient), because a 5s reachability timeout must
// never cut off a 2-minute apt-get or a 45s VM boot at the transport layer.
const (
	discoverTimeout = 5 * time.Second   // /vms reachability fan-out
	createTimeout   = 90 * time.Second  // server waits up to 45s for SSH on boot
	pauseTimeout    = 90 * time.Second  // snapshot dump
	resumeTimeout   = 120 * time.Second // fresh FC + snapshot load (may pull from S3)
	destroyTimeout  = 60 * time.Second
	restoreTimeout  = 600 * time.Second // S3 download of overlay+memory can be minutes
	drainTimeout    = 180 * time.Second // pauses every running VM on a node
)

// execClientTimeout is the HTTP ceiling for an exec call: strictly greater than the
// in-VM --timeout (plus margin for SSH setup + the round trip) so the command's own
// timeout — not the transport — is what bounds it.
func execClientTimeout(execTimeoutSecs int) time.Duration {
	return time.Duration(execTimeoutSecs)*time.Second + 30*time.Second
}
