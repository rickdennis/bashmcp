//go:build linux

package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"encoding/binary"
	"fmt"
	"log"
	"net"
	"net/http"
	"syscall"
	"time"
	"unsafe"
)

// tproxy.go (Linux only) — the data plane. Supports two interception modes:
//
//   - TPROXY (nftables): IP_TRANSPARENT lets LocalAddr() return the ORIGINAL destination.
//     Best when the kernel delivers TPROXY redirects to the socket (requires nft_tproxy and
//     correct prerouting hooks). Works on bare-metal and some VM setups.
//
//   - REDIRECT (iptables): kernel rewrites dst to 127.0.0.1:listen-port; the original
//     destination is recovered via SO_ORIGINAL_DST. More compatible — works reliably inside
//     kind/Docker where bridge traffic may bypass nftables prerouting hooks.
//
// The same listener handles both: if LocalAddr() matches our own listen addr the connection
// arrived via REDIRECT and we call SO_ORIGINAL_DST; otherwise we're in TPROXY mode and
// LocalAddr() already IS the original destination.

const soOriginalDst = 80 // SO_ORIGINAL_DST — not in stdlib syscall package

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
			_ = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_REUSEADDR, 1)
			_ = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, 15 /*SO_REUSEPORT*/, 1)
			// IP_TRANSPARENT: required for TPROXY mode (LocalAddr = original dst).
			// Harmless in REDIRECT mode (connections still arrive; we use SO_ORIGINAL_DST instead).
			serr = syscall.SetsockoptInt(int(fd), syscall.SOL_IP, syscall.IP_TRANSPARENT, 1)
		}); err != nil {
			return err
		}
		return serr
	}}
	ln, err := lc.Listen(ctx, "tcp", d.cfg.ListenAddr)
	if err != nil {
		return fmt.Errorf("tproxy/redirect listen %s: %w", d.cfg.ListenAddr, err)
	}
	defer ln.Close()
	log.Printf("egress: data plane listening on %s", d.cfg.ListenAddr)
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

	// Determine original destination. In TPROXY mode, IP_TRANSPARENT means LocalAddr() IS
	// the original destination. In REDIRECT mode, LocalAddr() is our own listen addr, so we
	// call SO_ORIGINAL_DST on the underlying fd to recover the real destination.
	origDst := d.originalDst(raw)
	if origDst == "" {
		log.Printf("egress: could not determine original dst for %s — dropping", raw.RemoteAddr())
		return
	}
	srcIP := hostOnly(raw.RemoteAddr().String())
	pol := d.resolver.Resolve(srcIP)
	if pol == nil {
		// Startup race: session was created just as fc-egress started; the poll may have
		// narrowly missed the new index entry. Force one reload and retry before denying.
		_ = d.resolver.(*fileIndexResolver).reloadIfChanged()
		pol = d.resolver.Resolve(srcIP)
	}

	var sni string
	tlsConn := tls.Server(raw, &tls.Config{
		NextProtos: []string{"http/1.1"}, // force HTTP/1.1; we don't MITM h2
		GetCertificate: func(hi *tls.ClientHelloInfo) (*tls.Certificate, error) {
			sni = hi.ServerName
			return d.certs.getCertificate(hi)
		},
	})
	if err := tlsConn.Handshake(); err != nil {
		log.Printf("egress: TLS handshake from %s failed: %v (is the egress CA trusted by the guest?)", srcIP, err)
		return
	}
	if sni == "" {
		log.Printf("egress: no SNI from %s — dropping", srcIP)
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
		req.RequestURI = ""
		if err := d.registry.Apply(context.Background(), sni, req, pol); err != nil {
			log.Printf("egress: denied %s %s/%s: %v", srcIP, sni, req.URL.Path, err)
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

// originalDst returns the original destination address for the connection. In TPROXY mode
// LocalAddr() already holds it (IP_TRANSPARENT). In REDIRECT mode LocalAddr() is the local
// listen address, so we read the kernel-saved original from SO_ORIGINAL_DST.
func (d *tproxyDataPlane) originalDst(conn net.Conn) string {
	local := conn.LocalAddr().String()
	if local != d.cfg.ListenAddr {
		// TPROXY mode: LocalAddr is the original destination.
		return local
	}
	// REDIRECT mode: recover original destination via SO_ORIGINAL_DST.
	tc, ok := conn.(*net.TCPConn)
	if !ok {
		return ""
	}
	var raw syscall.RawConn
	raw, err := tc.SyscallConn()
	if err != nil {
		return ""
	}
	var addr string
	_ = raw.Control(func(fd uintptr) {
		// SO_ORIGINAL_DST returns a sockaddr_in (16 bytes).
		b := make([]byte, 16)
		size := uint32(len(b))
		_, _, errno := syscall.Syscall6(
			syscall.SYS_GETSOCKOPT,
			fd,
			syscall.SOL_IP,
			soOriginalDst,
			uintptr(unsafe.Pointer(&b[0])),
			uintptr(unsafe.Pointer(&size)),
			0,
		)
		if errno != 0 {
			return
		}
		// sockaddr_in: [2 bytes family][2 bytes port BE][4 bytes IP]
		port := binary.BigEndian.Uint16(b[2:4])
		ip := net.IP(b[4:8])
		addr = fmt.Sprintf("%s:%d", ip, port)
	})
	return addr
}

// dialUpstream connects to the original destination. In DNAT mode the interception rule is
// already scoped to saddr 172.16.0.0/24 (VM traffic), so fc-egress's own outbound sockets
// never match it — no SO_MARK needed. SO_MARK is kept as a belt-and-suspenders guard for
// TPROXY mode (where the fwmark rule is broader), but only applied when cfg.Fwmark > 0.
func (d *tproxyDataPlane) dialUpstream(addr, sni string) (net.Conn, error) {
	dialer := &net.Dialer{
		Timeout: 15 * time.Second,
		Control: func(_, _ string, c syscall.RawConn) error {
			if d.cfg.Fwmark == 0 {
				return nil
			}
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
