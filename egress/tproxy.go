//go:build linux

package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"fmt"
	"log"
	"net"
	"net/http"
	"syscall"
	"time"
)

// tproxy.go (Linux only) — the data plane. A TPROXY listener accepts the guest's
// intercepted TLS connections (nftables diverts :443 here; with IP_TRANSPARENT the socket's
// local address is the ORIGINAL destination). For each connection it terminates TLS with a
// per-SNI leaf, runs the credential registry on every request, and re-originates a fresh TLS
// connection to the real upstream (system root validation, fwmark-tagged so it isn't
// re-intercepted). This file holds the only Linux-specific syscalls in the binary.

type tproxyDataPlane struct {
	cfg      *Config
	certs    *certCache
	resolver PolicyResolver
	registry *Registry
}

func newTProxyDataPlane(cfg *Config, certs *certCache, resolver PolicyResolver, reg *Registry) (DataPlane, error) {
	return &tproxyDataPlane{cfg: cfg, certs: certs, resolver: resolver, registry: reg}, nil
}

func (d *tproxyDataPlane) Serve(ctx context.Context) error {
	lc := net.ListenConfig{Control: func(_, _ string, c syscall.RawConn) error {
		var serr error
		if err := c.Control(func(fd uintptr) {
			if e := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_REUSEADDR, 1); e != nil {
				serr = e
				return
			}
			// SO_REUSEPORT lets a new fc-egress process bind the same port immediately after a
			// crash/restart without waiting for the kernel to release it (avoids "address already
			// in use" in CrashLoopBackOff scenarios).
			if e := syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, 15 /*SO_REUSEPORT*/, 1); e != nil {
				serr = e
				return
			}
			// IP_TRANSPARENT: accept connections addressed to other IPs and preserve the
			// original destination as the socket's local address.
			serr = syscall.SetsockoptInt(int(fd), syscall.SOL_IP, syscall.IP_TRANSPARENT, 1)
		}); err != nil {
			return err
		}
		return serr
	}}
	ln, err := lc.Listen(ctx, "tcp", d.cfg.ListenAddr)
	if err != nil {
		return fmt.Errorf("tproxy listen %s: %w", d.cfg.ListenAddr, err)
	}
	defer ln.Close()
	go func() { <-ctx.Done(); ln.Close() }()
	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-ctx.Done():
				return nil
			default:
				return err
			}
		}
		go d.handle(conn)
	}
}

func (d *tproxyDataPlane) handle(raw net.Conn) {
	defer raw.Close()
	origDst := raw.LocalAddr().String() // original destination (IP_TRANSPARENT)
	srcIP := hostOnly(raw.RemoteAddr().String())
	pol := d.resolver.Resolve(srcIP)

	var sni string
	tlsConn := tls.Server(raw, &tls.Config{
		NextProtos: []string{"http/1.1"}, // force HTTP/1.1; we don't MITM h2
		GetCertificate: func(hi *tls.ClientHelloInfo) (*tls.Certificate, error) {
			sni = hi.ServerName
			return d.certs.getCertificate(hi)
		},
	})
	if err := tlsConn.Handshake(); err != nil || sni == "" {
		return
	}

	up, err := d.dialUpstream(origDst, sni)
	if err != nil {
		log.Printf("egress: upstream dial %s (%s) failed: %v", origDst, sni, err)
		return
	}
	defer up.Close()

	cr, ur := bufio.NewReader(tlsConn), bufio.NewReader(up)
	for {
		req, err := http.ReadRequest(cr)
		if err != nil {
			return
		}
		req.Host = sni
		req.RequestURI = "" // required before writing as a client request
		if err := d.registry.Apply(context.Background(), sni, req, pol); err != nil {
			writeForbidden(tlsConn, err)
			return
		}
		if err := req.Write(up); err != nil {
			return
		}
		resp, err := http.ReadResponse(ur, req)
		if err != nil {
			return
		}
		closeAfter := resp.Close || req.Close
		if err := resp.Write(tlsConn); err != nil {
			resp.Body.Close()
			return
		}
		resp.Body.Close()
		if closeAfter {
			return
		}
	}
}

// dialUpstream connects to the original destination and does a real TLS handshake against the
// genuine upstream (system roots — fc-egress is the trust pivot, not the guest). The dialer is
// fwmark-tagged so the nft rule skips this (re-originated) traffic.
func (d *tproxyDataPlane) dialUpstream(addr, sni string) (net.Conn, error) {
	dialer := &net.Dialer{
		Timeout: 15 * time.Second,
		Control: func(_, _ string, c syscall.RawConn) error {
			var serr error
			if err := c.Control(func(fd uintptr) {
				serr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_MARK, d.cfg.Fwmark)
			}); err != nil {
				return err
			}
			return serr
		},
	}
	raw, err := dialer.Dial("tcp", addr)
	if err != nil {
		return nil, err
	}
	tc := tls.Client(raw, &tls.Config{ServerName: sni})
	if err := tc.HandshakeContext(context.Background()); err != nil {
		raw.Close()
		return nil, err
	}
	return tc, nil
}

func writeForbidden(w net.Conn, reason error) {
	body := "egress denied: " + reason.Error() + "\n"
	fmt.Fprintf(w, "HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s", len(body), body)
}

func hostOnly(addr string) string {
	if h, _, err := net.SplitHostPort(addr); err == nil {
		return h
	}
	return addr
}
