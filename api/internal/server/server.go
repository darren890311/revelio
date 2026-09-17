// Package server wires the HTTP routes for the gateway: it checks the cache,
// calls the worker on a miss, persists the result, and returns it.
package server

import (
	"context"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/gin-contrib/cors"
	"github.com/gin-gonic/gin"

	"github.com/darren890311/revelio/api/internal/store"
	"github.com/darren890311/revelio/api/internal/worker"
)

// How long to wait on the worker (a cold analysis is ~12s; leave headroom).
const workerTimeout = 90 * time.Second

type Server struct {
	cache   store.Cache
	worker  *worker.Client
	ttl     time.Duration
	log     *slog.Logger
	limiter Limiter
	limits  Limits
}

func New(cache store.Cache, w *worker.Client, ttl time.Duration, log *slog.Logger) *Server {
	return &Server{cache: cache, worker: w, ttl: ttl, log: log}
}

// WithLimits enables the per-IP rate limits and the global daily budget cap,
// backed by the given Limiter (the Redis store). Optional: without it the
// endpoint runs unguarded (used by unit tests).
func (s *Server) WithLimits(l Limiter, limits Limits) *Server {
	s.limiter = l
	s.limits = limits
	return s
}

type analyzeRequest struct {
	URL  string `json:"url"`
	HTML string `json:"html"` // optional: extension-supplied rendered page (worker skips Playwright)
}

func (s *Server) Router(allowedOrigin string) *gin.Engine {
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(corsMiddleware(allowedOrigin))
	r.GET("/healthz", s.healthz)
	r.POST("/analyze", s.analyze)
	return r
}

func (s *Server) healthz(c *gin.Context) {
	ctx, cancel := context.WithTimeout(c.Request.Context(), 2*time.Second)
	defer cancel()
	c.JSON(http.StatusOK, gin.H{"status": "ok", "db": s.cache.Ping(ctx) == nil})
}

func (s *Server) analyze(c *gin.Context) {
	var req analyzeRequest
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.URL) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing url"})
		return
	}
	if !strings.Contains(req.URL, "groupon.com/deals/") {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"error": "not a Groupon deal URL (expected groupon.com/deals/<slug>)"})
		return
	}

	ctx := c.Request.Context()

	// Per-IP rate limit first, before any work (a 429 here is the cheapest reply).
	if !s.allowRequest(c, ctx) {
		return
	}

	key := normalizeURL(req.URL)

	// Cache hit → serve the stored JSON straight through.
	if cached, hit, err := s.cache.Get(ctx, key); err != nil {
		s.log.Warn("cache lookup failed", "err", err) // non-fatal: fall through to the worker
	} else if hit {
		c.Header("X-Cache", "HIT")
		c.Data(http.StatusOK, "application/json", cached)
		return
	}

	// Miss → this is the only path that spends money. Check the global daily
	// budget cap before spending it, so a flood of unique URLs can't run up the
	// Claude bill even if it comes from many IPs.
	if !s.withinBudget(c, ctx) {
		return
	}

	// Ask the worker, bounded by a timeout.
	workerCtx, cancel := context.WithTimeout(ctx, workerTimeout)
	defer cancel()
	raw, status, err := s.worker.Analyze(workerCtx, req.URL, req.HTML)
	if err != nil {
		s.log.Error("worker call failed", "err", err)
		c.JSON(http.StatusBadGateway, gin.H{"error": "worker unreachable: " + err.Error()})
		return
	}
	if status != http.StatusOK {
		c.Data(status, "application/json", raw) // pass the worker's error through verbatim
		return
	}

	if err := s.cache.Put(ctx, key, raw, s.ttl); err != nil {
		s.log.Warn("cache store failed", "err", err) // non-fatal: still return the fresh result
	}

	c.Header("X-Cache", "MISS")
	c.Data(http.StatusOK, "application/json", raw)
}

func corsMiddleware(allowed string) gin.HandlerFunc {
	cfg := cors.DefaultConfig()
	cfg.AllowMethods = []string{"GET", "POST"}
	cfg.AllowHeaders = []string{"Origin", "Content-Type"}
	if allowed == "*" {
		cfg.AllowAllOrigins = true
	} else {
		cfg.AllowOrigins = strings.Split(allowed, ",")
	}
	return cors.New(cfg)
}
