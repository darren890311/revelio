// Package config loads runtime configuration from the environment.
package config

import (
	"errors"
	"os"
	"strconv"
	"time"
)

type Config struct {
	RedisURL      string
	WorkerURL     string
	Port          string
	AllowedOrigin string
	CacheTTL      time.Duration
	// Abuse guards (0 disables a guard). Tunable via env without a code change.
	RatePerMin  int // per-IP requests/minute
	RatePerDay  int // per-IP requests/day
	DailyBudget int // global worker calls (Claude spend) per day
}

// Load reads config from the environment, applying defaults. REDIS_URL is
// required; everything else has a sensible local-dev default.
func Load() (Config, error) {
	c := Config{
		RedisURL:      os.Getenv("REDIS_URL"),
		WorkerURL:     envOr("WORKER_URL", "http://127.0.0.1:8000"),
		Port:          envOr("PORT", "8080"),
		AllowedOrigin: envOr("ALLOWED_ORIGIN", "*"),
		CacheTTL:      24 * time.Hour,
		RatePerMin:    envInt("RATE_PER_MIN", 10),
		RatePerDay:    envInt("RATE_PER_DAY", 100),
		DailyBudget:   envInt("DAILY_BUDGET", 200),
	}
	if c.RedisURL == "" {
		return Config{}, errors.New("REDIS_URL is required")
	}
	return c, nil
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// envInt reads an int env var, falling back to def when unset or unparseable.
func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
