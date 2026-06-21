package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"os"
	"time"
)

// ca.go — the egress MITM certificate authority. Its private key never leaves the host
// netns (the node-agent injects only the public ca.crt into each guest's trust store).
// It signs the per-SNI leaves the proxy presents when terminating guest TLS (tls_mitm.go).

type CA struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
}

func randSerial() (*big.Int, error) {
	return rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
}

// generateCA creates a fresh self-signed ECDSA P-256 CA.
func generateCA() (*CA, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	serial, err := randSerial()
	if err != nil {
		return nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: "fc-egress MITM CA", Organization: []string{"fc-mcp"}},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().AddDate(10, 0, 0),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
		IsCA:                  true,
		MaxPathLenZero:        true,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return nil, err
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, err
	}
	return &CA{cert: cert, key: key}, nil
}

// certPEM returns the CA certificate in PEM (the only part shared with guests).
func (ca *CA) certPEM() []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: ca.cert.Raw})
}

// keyPEM returns the CA private key in PEM (host-only).
func (ca *CA) keyPEM() ([]byte, error) {
	der, err := x509.MarshalECPrivateKey(ca.key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der}), nil
}

// loadCA parses a CA from cert+key PEM.
func loadCA(certPEM, keyPEM []byte) (*CA, error) {
	cb, _ := pem.Decode(certPEM)
	if cb == nil {
		return nil, fmt.Errorf("invalid CA certificate PEM")
	}
	cert, err := x509.ParseCertificate(cb.Bytes)
	if err != nil {
		return nil, err
	}
	kb, _ := pem.Decode(keyPEM)
	if kb == nil {
		return nil, fmt.Errorf("invalid CA key PEM")
	}
	key, err := x509.ParseECPrivateKey(kb.Bytes)
	if err != nil {
		return nil, err
	}
	return &CA{cert: cert, key: key}, nil
}

// loadOrGenerateCA loads the CA from disk, or generates and persists one if absent.
// Returns generated=true when a new CA was created.
func loadOrGenerateCA(certPath, keyPath string) (ca *CA, generated bool, err error) {
	cb, errC := os.ReadFile(certPath)
	kb, errK := os.ReadFile(keyPath)
	if errC == nil && errK == nil {
		ca, err = loadCA(cb, kb)
		return ca, false, err
	}
	ca, err = generateCA()
	if err != nil {
		return nil, false, err
	}
	if err = os.WriteFile(certPath, ca.certPEM(), 0o644); err != nil {
		return nil, false, err
	}
	kp, err := ca.keyPEM()
	if err != nil {
		return nil, false, err
	}
	if err = os.WriteFile(keyPath, kp, 0o600); err != nil {
		return nil, false, err
	}
	return ca, true, nil
}
