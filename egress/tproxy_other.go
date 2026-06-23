//go:build !linux

package main

import "fmt"

// tproxy_other.go — non-Linux stub so the package compiles and unit-tests run on macOS.
// The data plane needs Linux kernel features (TPROXY/IP_TRANSPARENT, SO_MARK, nftables);
// the real implementation is in tproxy.go (//go:build linux). main never reaches Serve here.

func newTProxyDataPlane(cfg *Config, certs *certCache, resolver PolicyResolver, reg *Registry) (DataPlane, error) {
	return nil, fmt.Errorf("fc-egress data plane requires Linux (TPROXY); this platform is unsupported")
}
