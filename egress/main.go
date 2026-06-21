package main

import (
	"context"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

// main.go — wires the control-plane components to the data plane: load/generate the egress
// CA, build the GitHub token source (octo-sts default; app-mint drop-in), start the
// egress-index poll loop and the health endpoint, then run the TPROXY data plane.

func main() {
	cfg := loadConfig(os.Args[1:])

	if cfg.GenerateCAOnly {
		_, gen, err := loadOrGenerateCA(cfg.CACertPath, cfg.CAKeyPath)
		if err != nil {
			log.Fatalf("generate CA: %v", err)
		}
		log.Printf("egress CA ready at %s (generated=%v)", cfg.CACertPath, gen)
		return
	}

	ca, gen, err := loadOrGenerateCA(cfg.CACertPath, cfg.CAKeyPath)
	if err != nil {
		log.Fatalf("load/generate CA: %v", err)
	}
	log.Printf("egress CA at %s (generated=%v)", cfg.CACertPath, gen)

	ts, err := buildTokenSource(cfg)
	if err != nil {
		log.Fatalf("token source: %v", err)
	}
	registry := NewRegistry(NewGitHubAdapter(newCachingTokenSource(ts, cfg.TokenSkew)))
	resolver := newFileIndexResolver(cfg.IndexPath)
	certs := newCertCache(ca)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	go pollIndex(ctx, resolver, cfg.PollInterval)
	go serveHealth(ctx, cfg.HealthAddr)

	dp, err := newTProxyDataPlane(cfg, certs, resolver, registry)
	if err != nil {
		log.Fatalf("data plane: %v", err)
	}
	log.Printf("fc-egress listening on %s (backend=%s, fwmark=%d, index=%s)",
		cfg.ListenAddr, cfg.GitHubBackend, cfg.Fwmark, cfg.IndexPath)
	if err := dp.Serve(ctx); err != nil {
		log.Fatalf("serve: %v", err)
	}
}

func pollIndex(ctx context.Context, r *fileIndexResolver, every time.Duration) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := r.reloadIfChanged(); err != nil {
				log.Printf("egress-index reload: %v", err)
			}
		}
	}
}

func serveHealth(ctx context.Context, addr string) {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	srv := &http.Server{Addr: addr, Handler: mux}
	go func() { <-ctx.Done(); _ = srv.Close() }()
	_ = srv.ListenAndServe()
}

// buildTokenSource selects the GitHub credential backend.
func buildTokenSource(cfg *Config) (TokenSource, error) {
	switch cfg.GitHubBackend {
	case "appmint":
		if cfg.AppID == 0 || cfg.AppKeyPath == "" {
			return nil, fmt.Errorf("appmint backend requires --app-id and --app-key")
		}
		keyPEM, err := os.ReadFile(cfg.AppKeyPath)
		if err != nil {
			return nil, err
		}
		key, err := parseRSAPrivateKey(keyPEM)
		if err != nil {
			return nil, err
		}
		return newAppMintTokenSource(cfg.AppID, key, ""), nil
	case "octosts":
		if cfg.OctoStsURL == "" {
			return nil, fmt.Errorf("octosts backend requires --octosts-url")
		}
		// The session-identity OIDC issuer is the plan's flagged open item (tied to the
		// identity work). Until it's wired, exchanges fail with a clear message; use
		// --github-backend=appmint for a fully working path today.
		mint := func(_ context.Context, _ string) (string, error) {
			return "", fmt.Errorf("octo-sts OIDC issuer not configured (open item); use --github-backend=appmint")
		}
		return newOctoStsTokenSource(cfg.OctoStsURL, cfg.OctoStsPolicy, mint), nil
	default:
		return nil, fmt.Errorf("unknown --github-backend %q (want octosts|appmint)", cfg.GitHubBackend)
	}
}

// parseRSAPrivateKey parses a PKCS#1 or PKCS#8 RSA private key from PEM.
func parseRSAPrivateKey(pemBytes []byte) (*rsa.PrivateKey, error) {
	b, _ := pem.Decode(pemBytes)
	if b == nil {
		return nil, fmt.Errorf("invalid PEM (no key block)")
	}
	if k, err := x509.ParsePKCS1PrivateKey(b.Bytes); err == nil {
		return k, nil
	}
	k, err := x509.ParsePKCS8PrivateKey(b.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse RSA key: %w", err)
	}
	rk, ok := k.(*rsa.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("PEM is not an RSA private key (%T)", k)
	}
	return rk, nil
}
