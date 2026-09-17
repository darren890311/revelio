package server

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"

	"github.com/darren890311/revelio/api/internal/worker"
)

// fakeLimiter counts increments per key in memory, like Redis INCR would.
type fakeLimiter struct {
	mu     sync.Mutex
	counts map[string]int64
}

func newFakeLimiter() *fakeLimiter { return &fakeLimiter{counts: map[string]int64{}} }

func (f *fakeLimiter) Incr(_ context.Context, key string, _ time.Duration) (int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.counts[key]++
	return f.counts[key], nil
}

func limitedRouter(cache *fakeCache, workerURL string, limits Limits) *gin.Engine {
	gin.SetMode(gin.TestMode)
	s := New(cache, worker.New(workerURL), time.Hour, slog.New(slog.NewTextHandler(io.Discard, nil))).
		WithLimits(newFakeLimiter(), limits)
	return s.Router("*")
}

const dealBody = `{"url":"https://www.groupon.com/deals/x"}`

func TestPerIPRateLimit_Returns429(t *testing.T) {
	// Cache hits so no worker is needed; only the per-IP minute limit is active.
	cache := &fakeCache{getRaw: json.RawMessage(`{"ok":true}`), getHit: true}
	router := limitedRouter(cache, "http://unused", Limits{PerMinute: 2})

	codes := []int{}
	for i := 0; i < 3; i++ {
		codes = append(codes, postAnalyze(router, dealBody).Code)
	}
	// 2 allowed, the 3rd over the per-minute cap → 429.
	if codes[0] != http.StatusOK || codes[1] != http.StatusOK {
		t.Fatalf("first two = %v, want 200,200", codes[:2])
	}
	if codes[2] != http.StatusTooManyRequests {
		t.Fatalf("third = %d, want 429", codes[2])
	}
}

func TestDailyBudgetCap_Returns503_AndSkipsWorkerWhenSpent(t *testing.T) {
	var workerCalls int
	ws := httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, _ *http.Request) {
		workerCalls++
		rw.Header().Set("Content-Type", "application/json")
		_, _ = rw.Write([]byte(`{"fresh":true}`))
	}))
	defer ws.Close()

	// Cache misses so every request would reach the worker; budget of 1 means
	// the first miss spends it and the second is refused before the worker.
	cache := &fakeCache{getHit: false}
	router := limitedRouter(cache, ws.URL, Limits{DailyBudget: 1})

	if got := postAnalyze(router, dealBody).Code; got != http.StatusOK {
		t.Fatalf("first = %d, want 200", got)
	}
	if got := postAnalyze(router, dealBody).Code; got != http.StatusServiceUnavailable {
		t.Fatalf("second = %d, want 503", got)
	}
	if workerCalls != 1 {
		t.Fatalf("worker called %d times, want 1 (budget must block the 2nd before the worker)", workerCalls)
	}
}
