package main

import (
	"crypto/x509"
	"os"
	"path/filepath"
	"testing"
)

func TestGenerateCA(t *testing.T) {
	ca, err := generateCA()
	if err != nil {
		t.Fatalf("generateCA: %v", err)
	}
	if !ca.cert.IsCA {
		t.Errorf("CA cert IsCA = false, want true")
	}
	if ca.cert.KeyUsage&x509.KeyUsageCertSign == 0 {
		t.Errorf("CA cert must have the certSign key usage")
	}
}

func TestCA_PEMRoundTrip(t *testing.T) {
	ca, err := generateCA()
	if err != nil {
		t.Fatal(err)
	}
	keyPEM, err := ca.keyPEM()
	if err != nil {
		t.Fatal(err)
	}
	got, err := loadCA(ca.certPEM(), keyPEM)
	if err != nil {
		t.Fatalf("loadCA: %v", err)
	}
	if got.cert.SerialNumber.Cmp(ca.cert.SerialNumber) != 0 {
		t.Errorf("round-tripped CA serial mismatch")
	}
}

func TestLoadOrGenerateCA(t *testing.T) {
	dir := t.TempDir()
	cp := filepath.Join(dir, "ca.crt")
	kp := filepath.Join(dir, "ca.key")

	ca1, gen, err := loadOrGenerateCA(cp, kp)
	if err != nil {
		t.Fatal(err)
	}
	if !gen {
		t.Errorf("first call should generate a new CA")
	}
	if _, err := os.Stat(cp); err != nil {
		t.Errorf("ca.crt should have been written: %v", err)
	}

	ca2, gen2, err := loadOrGenerateCA(cp, kp)
	if err != nil {
		t.Fatal(err)
	}
	if gen2 {
		t.Errorf("second call should load the existing CA, not generate")
	}
	if ca1.cert.SerialNumber.Cmp(ca2.cert.SerialNumber) != 0 {
		t.Errorf("loaded CA should match the generated one")
	}
}
