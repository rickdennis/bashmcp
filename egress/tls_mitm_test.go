package main

import (
	"crypto/tls"
	"crypto/x509"
	"testing"
)

func TestCertCache_SignsLeafForSNI(t *testing.T) {
	ca, err := generateCA()
	if err != nil {
		t.Fatal(err)
	}
	cc := newCertCache(ca)
	crt, err := cc.leafFor("github.com")
	if err != nil {
		t.Fatalf("leafFor: %v", err)
	}

	found := false
	for _, n := range crt.Leaf.DNSNames {
		if n == "github.com" {
			found = true
		}
	}
	if !found {
		t.Errorf("leaf DNSNames = %v, want to include github.com", crt.Leaf.DNSNames)
	}

	// The leaf must chain to the egress CA (this is what the guest validates).
	roots := x509.NewCertPool()
	roots.AddCert(ca.cert)
	if _, err := crt.Leaf.Verify(x509.VerifyOptions{DNSName: "github.com", Roots: roots}); err != nil {
		t.Errorf("leaf should verify against the CA: %v", err)
	}
}

func TestCertCache_Caches(t *testing.T) {
	ca, _ := generateCA()
	cc := newCertCache(ca)
	a, _ := cc.leafFor("github.com")
	b, _ := cc.leafFor("github.com")
	if a != b {
		t.Errorf("leafFor should return the cached cert on repeat call")
	}
}

func TestCertCache_NoSNIErrors(t *testing.T) {
	ca, _ := generateCA()
	cc := newCertCache(ca)
	if _, err := cc.leafFor(""); err == nil {
		t.Errorf("an empty SNI should error")
	}
}

func TestCertCache_GetCertificateUsesSNI(t *testing.T) {
	ca, _ := generateCA()
	cc := newCertCache(ca)
	crt, err := cc.getCertificate(&tls.ClientHelloInfo{ServerName: "github.com"})
	if err != nil {
		t.Fatalf("getCertificate: %v", err)
	}
	if crt.Leaf.Subject.CommonName != "github.com" {
		t.Errorf("leaf CN = %q, want github.com", crt.Leaf.Subject.CommonName)
	}
}
