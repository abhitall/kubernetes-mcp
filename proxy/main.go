// Package main implements a lightweight Kubernetes API reverse proxy.
//
// It runs inside a Kubernetes cluster and proxies HTTP requests to the
// cluster's API server, authenticating with the pod's mounted service
// account token. This allows external MCP servers to access the K8s API
// without direct API server exposure or token management.
//
// Flow:
//
//	MCP Server ──HTTP──▸ k8s-api-proxy (this) ──HTTPS+SA token──▸ K8s API Server
//
// Security:
//   - Authenticates to the API server using the auto-rotated SA token
//   - Optional shared secret (PROXY_AUTH_TOKEN) gates external access
//   - Optional read-only mode rejects mutating HTTP methods
//   - Structured logging with no credential leakage
package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"
)

// Default paths for in-cluster credentials.
const (
	defaultTokenPath  = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	defaultCACertPath = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
)

// config holds the proxy configuration.
type config struct {
	listenAddr    string
	apiServer     string
	tokenPath     string
	caCertPath    string
	authToken     string // optional shared secret for proxy access
	readOnly      bool
	skipTLSVerify bool
}

func main() {
	cfg := parseFlags()

	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	transport, err := buildTransport(cfg)
	if err != nil {
		slog.Error("failed to build TLS transport", "error", err)
		os.Exit(1)
	}

	tokenProvider := newTokenProvider(cfg.tokenPath)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", healthzHandler)
	mux.HandleFunc("/readyz", readyzHandler(cfg, transport, tokenProvider))
	mux.HandleFunc("/", proxyHandler(cfg, transport, tokenProvider))

	srv := &http.Server{
		Addr:              cfg.listenAddr,
		Handler:           mux,
		ReadTimeout:       30 * time.Second,
		ReadHeaderTimeout: 10 * time.Second,
		WriteTimeout:      120 * time.Second,
		IdleTimeout:       90 * time.Second,
		MaxHeaderBytes:    1 << 20, // 1 MB
	}

	// Graceful shutdown
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		slog.Info("starting k8s-api-proxy",
			"addr", cfg.listenAddr,
			"api_server", cfg.apiServer,
			"read_only", cfg.readOnly,
			"auth_enabled", cfg.authToken != "",
		)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			slog.Error("server failed", "error", err)
			os.Exit(1)
		}
	}()

	<-ctx.Done()
	slog.Info("shutting down gracefully")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Error("shutdown error", "error", err)
	}
}

// parseFlags reads configuration from flags and environment variables.
func parseFlags() config {
	cfg := config{}

	flag.StringVar(&cfg.listenAddr, "listen", envOrDefault("PROXY_LISTEN", ":8443"), "Address to listen on")
	flag.StringVar(&cfg.apiServer, "api-server", envOrDefault("KUBERNETES_API_SERVER", autoDetectAPIServer()), "Kubernetes API server URL")
	flag.StringVar(&cfg.tokenPath, "token-path", envOrDefault("SA_TOKEN_PATH", defaultTokenPath), "Path to service account token")
	flag.StringVar(&cfg.caCertPath, "ca-cert-path", envOrDefault("SA_CA_CERT_PATH", defaultCACertPath), "Path to CA certificate")
	flag.StringVar(&cfg.authToken, "auth-token", os.Getenv("PROXY_AUTH_TOKEN"), "Optional shared secret for proxy access")
	flag.BoolVar(&cfg.readOnly, "read-only", envOrDefault("PROXY_READ_ONLY", "false") == "true", "Reject mutating requests")
	flag.BoolVar(&cfg.skipTLSVerify, "skip-tls-verify", envOrDefault("SKIP_TLS_VERIFY", "false") == "true", "Skip TLS verification (dev only)")
	flag.Parse()

	return cfg
}

// autoDetectAPIServer returns the in-cluster API server URL.
func autoDetectAPIServer() string {
	host := os.Getenv("KUBERNETES_SERVICE_HOST")
	port := os.Getenv("KUBERNETES_SERVICE_PORT")
	if host != "" && port != "" {
		// Handle IPv6
		if strings.Contains(host, ":") {
			return fmt.Sprintf("https://[%s]:%s", host, port)
		}
		return fmt.Sprintf("https://%s:%s", host, port)
	}
	return "https://kubernetes.default.svc:443"
}

func envOrDefault(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

// buildTransport creates an HTTP transport with the in-cluster CA certificate.
func buildTransport(cfg config) (*http.Transport, error) {
	tlsCfg := &tls.Config{
		MinVersion: tls.VersionTLS12,
	}

	if cfg.skipTLSVerify {
		tlsCfg.InsecureSkipVerify = true //nolint:gosec // dev only flag
		slog.Warn("TLS verification disabled — for development only")
	} else {
		caCert, err := os.ReadFile(cfg.caCertPath)
		if err != nil {
			// Fall back to system CA pool if in-cluster cert is missing
			slog.Warn("CA cert not found, using system CA pool", "path", cfg.caCertPath, "error", err)
		} else {
			pool := x509.NewCertPool()
			if !pool.AppendCertsFromPEM(caCert) {
				return nil, fmt.Errorf("failed to parse CA certificate from %s", cfg.caCertPath)
			}
			tlsCfg.RootCAs = pool
		}
	}

	return &http.Transport{
		TLSClientConfig:     tlsCfg,
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 100,
		IdleConnTimeout:     90 * time.Second,
		TLSHandshakeTimeout: 10 * time.Second,
	}, nil
}

// tokenProvider reads the SA token from disk on each request to handle rotation.
type tokenProvider struct {
	path  string
	mu    sync.RWMutex
	token string
	mtime time.Time
}

func newTokenProvider(path string) *tokenProvider {
	return &tokenProvider{path: path}
}

func (tp *tokenProvider) Token() (string, error) {
	info, err := os.Stat(tp.path)
	if err != nil {
		return "", fmt.Errorf("token file not found: %w", err)
	}

	tp.mu.RLock()
	if tp.token != "" && info.ModTime().Equal(tp.mtime) {
		defer tp.mu.RUnlock()
		return tp.token, nil
	}
	tp.mu.RUnlock()

	tp.mu.Lock()
	defer tp.mu.Unlock()

	data, err := os.ReadFile(tp.path)
	if err != nil {
		return "", fmt.Errorf("failed to read token: %w", err)
	}

	tp.token = strings.TrimSpace(string(data))
	tp.mtime = info.ModTime()
	return tp.token, nil
}

// mutatingMethods are HTTP methods that modify state.
var mutatingMethods = map[string]bool{
	"POST":   true,
	"PUT":    true,
	"PATCH":  true,
	"DELETE": true,
}

// proxyHandler creates the main reverse proxy handler.
func proxyHandler(cfg config, transport *http.Transport, tp *tokenProvider) http.HandlerFunc {
	client := &http.Client{
		Transport: transport,
		Timeout:   120 * time.Second,
		// Don't follow redirects — let the caller see them
		CheckRedirect: func(req *http.Request, via []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}

	return func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()

		// Auth check
		if cfg.authToken != "" {
			if r.Header.Get("X-Proxy-Token") != cfg.authToken {
				http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
				slog.Warn("unauthorized request", "remote", r.RemoteAddr, "path", r.URL.Path)
				return
			}
		}

		// Read-only check
		if cfg.readOnly && mutatingMethods[r.Method] {
			http.Error(w, `{"error":"proxy is in read-only mode"}`, http.StatusForbidden)
			return
		}

		// Get SA token
		token, err := tp.Token()
		if err != nil {
			http.Error(w, fmt.Sprintf(`{"error":"token unavailable: %s"}`, err), http.StatusBadGateway)
			slog.Error("token unavailable", "error", err)
			return
		}

		// Build upstream URL
		upstreamURL := cfg.apiServer + r.URL.RequestURI()

		// Create upstream request
		upReq, err := http.NewRequestWithContext(r.Context(), r.Method, upstreamURL, r.Body)
		if err != nil {
			http.Error(w, `{"error":"failed to create upstream request"}`, http.StatusInternalServerError)
			slog.Error("request creation failed", "error", err)
			return
		}

		// Copy headers (skip hop-by-hop)
		copyHeaders(r.Header, upReq.Header)
		upReq.Header.Set("Authorization", "Bearer "+token)
		// Don't forward the proxy auth token upstream
		upReq.Header.Del("X-Proxy-Token")

		// Execute
		resp, err := client.Do(upReq)
		if err != nil {
			http.Error(w, fmt.Sprintf(`{"error":"upstream request failed: %s"}`, err), http.StatusBadGateway)
			slog.Error("upstream error", "error", err, "method", r.Method, "path", r.URL.Path)
			return
		}
		defer resp.Body.Close()

		// Copy response headers
		copyHeaders(resp.Header, w.Header())
		w.WriteHeader(resp.StatusCode)
		io.Copy(w, resp.Body) //nolint:errcheck

		slog.Info("proxied",
			"method", r.Method,
			"path", r.URL.Path,
			"status", resp.StatusCode,
			"duration_ms", time.Since(start).Milliseconds(),
		)
	}
}

// hopByHop headers that should not be forwarded.
var hopByHop = map[string]bool{
	"Connection":          true,
	"Keep-Alive":          true,
	"Proxy-Authenticate":  true,
	"Proxy-Authorization": true,
	"Te":                  true,
	"Trailers":            true,
	"Transfer-Encoding":   true,
	"Upgrade":             true,
}

func copyHeaders(src, dst http.Header) {
	for k, vv := range src {
		if hopByHop[k] {
			continue
		}
		for _, v := range vv {
			dst.Add(k, v)
		}
	}
}

// healthzHandler returns a simple liveness probe response.
func healthzHandler(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	fmt.Fprint(w, `{"status":"ok"}`)
}

// readyzHandler checks connectivity to the API server.
func readyzHandler(cfg config, transport *http.Transport, tp *tokenProvider) http.HandlerFunc {
	client := &http.Client{Transport: transport, Timeout: 5 * time.Second}

	return func(w http.ResponseWriter, r *http.Request) {
		token, err := tp.Token()
		if err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintf(w, `{"status":"not ready","error":"%s"}`, err)
			return
		}

		req, _ := http.NewRequestWithContext(r.Context(), "GET", cfg.apiServer+"/version", nil)
		req.Header.Set("Authorization", "Bearer "+token)

		resp, err := client.Do(req)
		if err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintf(w, `{"status":"not ready","error":"%s"}`, err)
			return
		}
		resp.Body.Close()

		if resp.StatusCode == http.StatusOK {
			w.Header().Set("Content-Type", "application/json")
			fmt.Fprintf(w, `{"status":"ready","api_server":"%s"}`, cfg.apiServer)
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintf(w, `{"status":"not ready","api_server_status":%d}`, resp.StatusCode)
		}
	}
}
