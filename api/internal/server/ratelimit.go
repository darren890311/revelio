package server

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
)

// Limiter is the atomic counter the abuse guards need. *store.Redis implements
// it; when nil (e.g. in unit tests) the guards are simply skipped.
type Limiter interface {
	Incr(ctx context.Context, key string, window time.Duration) (int64, error)
}

// Limits configures the abuse guards. A zero value disables that guard.
//
// The worker is private (only this gateway can reach it), so this gateway is the
// single chokepoint - which is exactly why the limits live here:
//   - PerMinute / PerDay cap a single caller (per IP), stopping one script from
//     hammering the endpoint.
//   - DailyBudget caps the GLOBAL number of worker calls (cache misses = actual
//     Claude spend) per day, as a spend fuse that holds even if per-IP limits are
//     dodged with many IPs. Cache hits are free and never counted against it.
type Limits struct {
	PerMinute   int
	PerDay      int
	DailyBudget int
}

func minuteKey(ip string, now time.Time) string {
	return fmt.Sprintf("rl:min:%s:%d", ip, now.Unix()/60)
}

func dayKey(ip string, now time.Time) string {
	return fmt.Sprintf("rl:day:%s:%s", ip, now.Format("20060102"))
}

func budgetKey(now time.Time) string {
	return fmt.Sprintf("budget:%s", now.Format("20060102"))
}

// allowRequest enforces the per-IP rate limits. It returns false and writes a
// 429 when the caller is over a limit. A Redis error fails OPEN (we serve the
// request) - availability over strictness, since the global DailyBudget still
// backstops cost.
func (s *Server) allowRequest(c *gin.Context, ctx context.Context) bool {
	if s.limiter == nil {
		return true
	}
	ip, now := c.ClientIP(), time.Now()

	if s.limits.PerMinute > 0 {
		if n, err := s.limiter.Incr(ctx, minuteKey(ip, now), 2*time.Minute); err == nil && n > int64(s.limits.PerMinute) {
			c.Header("Retry-After", "60")
			c.JSON(http.StatusTooManyRequests, gin.H{"error": "Too many requests - please slow down and try again in a minute."})
			return false
		}
	}
	if s.limits.PerDay > 0 {
		if n, err := s.limiter.Incr(ctx, dayKey(ip, now), 48*time.Hour); err == nil && n > int64(s.limits.PerDay) {
			c.JSON(http.StatusTooManyRequests, gin.H{"error": "Daily request limit reached. Please try again tomorrow."})
			return false
		}
	}
	return true
}

// withinBudget enforces the global daily worker-call budget. Call it ONLY on a
// cache miss, right before invoking the worker, so cache hits stay free. Returns
// false and writes a 503 when the day's budget is spent. A Redis error fails
// OPEN so a limiter blip can't take the whole service down.
func (s *Server) withinBudget(c *gin.Context, ctx context.Context) bool {
	if s.limiter == nil || s.limits.DailyBudget <= 0 {
		return true
	}
	if n, err := s.limiter.Incr(ctx, budgetKey(time.Now()), 48*time.Hour); err == nil && n > int64(s.limits.DailyBudget) {
		c.JSON(http.StatusServiceUnavailable, gin.H{"error": "Revelio has hit today's analysis limit. Please try again tomorrow."})
		return false
	}
	return true
}
