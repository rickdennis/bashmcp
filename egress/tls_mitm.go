package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"fmt"
	"sync"
	"time"
)

// tls_mitm.go — the TLS interception helper. certCache hands the MITM server a leaf
// certificate for each requested SNI (generated on demand, signed by the egress CA,
// cached), wired in as tls.Config.GetCertificate. Re-origination to the real upstream
// (using the original destination and the system root store) lives in tproxy.go.

type certCache struct {
	ca    *CA
	mu    sync.Mutex
	cache map[string]*tls.Certificate
}

func newCertCache(ca *CA) *certCache {
	return &certCache{ca: ca, cache: map[string]*tls.Certificate{}}
}

// getCertificate is the tls.Config.GetCertificate hook: it picks a leaf by ClientHello SNI.
func (c *certCache) getCertificate(hello *tls.ClientHelloInfo) (*tls.Certificate, error) {
	return c.leafFor(hello.ServerName)
}

// leafFor returns a (cached) leaf certificate for the given SNI, signed by the egress CA.
func (c *certCache) leafFor(sni string) (*tls.Certificate, error) {
	if sni == "" {
		return nil, fmt.Errorf("no SNI in ClientHello; cannot MITM")
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if crt, ok := c.cache[sni]; ok {
		return crt, nil
	}
	crt, err := c.ca.signLeaf(sni)
	if err != nil {
		return nil, err
	}
	c.cache[sni] = crt
	return crt, nil
}

// signLeaf mints a short-lived server leaf for sni, signed by the CA.
func (ca *CA) signLeaf(sni string) (*tls.Certificate, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	serial, err := randSerial()
	if err != nil {
		return nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: sni},
		DNSNames:     []string{sni},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.cert, &key.PublicKey, ca.key)
	if err != nil {
		return nil, err
	}
	leaf, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, err
	}
	return &tls.Certificate{
		Certificate: [][]byte{der, ca.cert.Raw},
		PrivateKey:  key,
		Leaf:        leaf,
	}, nil
}
