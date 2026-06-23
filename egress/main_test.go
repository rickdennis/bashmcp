package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"testing"
)

func TestDenyTokenSource(t *testing.T) {
	if _, _, err := (denyTokenSource{fmt.Errorf("nope")}).Token(context.Background(), "s", "o", "r"); err == nil {
		t.Errorf("denyTokenSource must always error (fail closed)")
	}
}

func TestParseRSAPrivateKey_PKCS1(t *testing.T) {
	k, _ := rsa.GenerateKey(rand.Reader, 2048)
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(k)})
	got, err := parseRSAPrivateKey(pemBytes)
	if err != nil {
		t.Fatalf("PKCS1 parse: %v", err)
	}
	if got.N.Cmp(k.N) != 0 {
		t.Errorf("parsed PKCS1 key does not match original")
	}
}

func TestParseRSAPrivateKey_PKCS8(t *testing.T) {
	k, _ := rsa.GenerateKey(rand.Reader, 2048)
	der, _ := x509.MarshalPKCS8PrivateKey(k)
	pemBytes := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
	got, err := parseRSAPrivateKey(pemBytes)
	if err != nil {
		t.Fatalf("PKCS8 parse: %v", err)
	}
	if got.N.Cmp(k.N) != 0 {
		t.Errorf("parsed PKCS8 key does not match original")
	}
}

func TestParseRSAPrivateKey_Invalid(t *testing.T) {
	if _, err := parseRSAPrivateKey([]byte("not a pem")); err == nil {
		t.Errorf("invalid PEM should error")
	}
}
