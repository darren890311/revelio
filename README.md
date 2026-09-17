# Revelio - Is This Deal Actually A Deal?

Revelio tells you whether a Groupon deal is actually a good buy. It's a **Chrome
extension** that pops up on any Groupon deal page, reads the live page, checks the
**real** discount against the advertised one, compares prices to similar same-city deals,
cross-references the merchant's **Google** rating, and checks whether booking direct is
cheaper - returning a clear **buy / caution / skip** verdict, every claim grounded in
real data, not vibes.

Revelio is an **independent tool - not affiliated with or endorsed by Groupon.**

**▶︎ Live site: https://getrevelio.web.app** · [Privacy policy](https://getrevelio.web.app/privacy.html)

---

## Why it exists

Groupon deal pages routinely advertise a headline discount ("Up to 50% Off") that's
larger than what the price tiers actually show, hide cheaper equivalents a few clicks
away, and lean on a thin on-platform review count. Revelio does the legwork a careful
shopper can't do at scale and gives a straight answer.

## What it checks

| Signal | What it does |
| --- | --- |
| **Discount truth** | Parses the advertised claim from the title and compares it to the real strike-through discount on each price tier. Flags `genuine` / `exaggerated` / `none`. |
| **Like-for-like price** | Finds same-city deals on Groupon and uses an LLM to judge which are the *same* service vs merely *similar* - refusing to compare across scope (a 3-attraction pass is not "the same" as a 5-attraction one, even under the same brand). |
| **Reputation** | Pulls the merchant's structured rating from the **Google Places** API and compares it to the Groupon score. A large, well-sampled gap is flagged as *ratings disagree*; multi-location chains are flagged as *varies by location* rather than guessed. |
| **Direct booking** | Checks whether booking the merchant directly (or via an official reseller) beats the Groupon price - comparing only same-scope prices, and staying silent when it's too close to call. |
| **Verdict** | Combines the above into deterministic badges + an LLM-written one-liner and recommended action. Revelio is neutral: it has no buy button and earns nothing on a purchase. |

## Architecture

One analyzer (the extension) plus a marketing landing page, one backend:

```
┌─ Chrome extension (WXT + Vue 3) ─┐     ┌─ Landing page (Vue 3) ─┐
│ reads the deal page in-browser,  │     │ marketing only,        │
│ POST /analyze { url, html }      │     │ links to the extension │
└───────────────┬──────────────────┘     └────────────────────────┘
                ▼
            Go / Gin gateway  ──────────►  Upstash Redis  (cache by URL, 24h TTL)
                │  identity-token auth
                ▼
            Python worker (FastAPI, private, Cloud Run)
              parse (BeautifulSoup: JSON-LD ProductGroup + the rendered price DOM via data-testid)
              → discount math → competitors (Claude) → reputation (Google Places)
              → direct booking (Tavily + Claude) → verdict (Claude)
              ↑ headless-Chromium scrape (Playwright) ONLY when the extension can't supply page HTML
```

- **The extension reads the page client-side.** It sends the rendered HTML (and reads
  the on-page *Similar deals* for competitors), so the worker normally **skips Playwright
  entirely** - no headless-browser launch, no bot-challenge, no datacenter-IP geography
  issue. Playwright is only a fallback for the rare case where the extension can't supply
  trusted HTML (see notes). The landing page is Vue 3 too, but it's marketing only - it
  doesn't call the backend.
- **Stateless Python worker** does the analysis (LLM calls, and scraping only on the
  fallback path). It's **private** on Cloud Run - only the Go gateway can reach it via a
  Google-signed identity token.
- **Go gateway** is the only stateful layer: it owns the Redis cache and is the public
  entry point. Right-sized tool per layer - Go for the low-latency edge, Python for ML.
- The three research stages (competitors, reputation, direct booking) run
  **concurrently**; only the final verdict waits on all of them.

## Tech stack

- **Front-ends:** **Vue 3** + Vite - a **Chrome MV3 extension** built with **WXT** (panel
  mounted in a Shadow DOM), plus a marketing landing page on **Firebase Hosting**
- **API gateway:** **Go** (Gin, go-redis), multi-stage Dockerfile, on **Cloud Run**
- **Worker:** **Python** (FastAPI, Playwright, BeautifulSoup), Dockerized, on **Cloud Run** (private)
- **Data:** **Upstash** (serverless Redis) for the analysis cache - URL key → result with a native TTL
- **AI/search:** **Claude** (Anthropic) for judgment + structured output, **Tavily** for web search
- **External APIs:** **Google Places** for the merchant's structured rating
- **Infra:** GCP (Cloud Run, Cloud Build, Artifact Registry, IAM), Docker, service-to-service auth

## Engineering notes

A few decisions worth calling out:

- **One backend, two clients - and the extension makes scraping client-side.** The
  extension reads the deal page in the user's own browser (real IP, page already
  rendered) and posts the HTML, so the worker doesn't run headless Chromium for it.
- **Stale page on SPA navigation is detected.** Groupon is a single-page app: navigating
  deal→deal updates the DOM but the server-rendered markup can lag the first deal. The
  extension only sends the page when the JSON-LD `ProductGroup` slug matches the URL;
  otherwise it sends URL-only and the worker fetches the deal fresh. (Groupon migrated off
  Next.js to an Apollo/GraphQL SPA and dropped the old `__NEXT_DATA__` blob, so the slug
  check now reads the JSON-LD block that is still server-rendered for SEO.)
- **Pricing is read from the rendered DOM, not JSON-LD.** Since the SPA migration, JSON-LD
  is unreliable for price: on a promo deal `offers.price`/`priceSpecification` carry the
  Groupon and with-code prices in inconsistent roles, and the true strike-through anchor
  (e.g. `$258` before a `$85.14` deal) is absent entirely. The worker instead reads the
  prices the shopper sees via Groupon's stable `data-testid`s (`strike-through-price`,
  `green-price`), and joins the JSON-LD option labels back by matching the deal price.
- **Judgments refuse cross-scope comparisons.** Comparability and direct-booking treat a
  different quantity/coverage (3 vs 5 attractions, a smaller package) as at most
  *similar*, never *same*, even under a matching brand. A full-star, well-sampled
  rating gap downgrades the verdict to *caution*. Decisions (who's cheaper, worth-buying)
  are computed in code; the LLM only extracts and narrates.
- **The worker is private.** `allUsers` invoke access is removed; the gateway
  authenticates with an OIDC identity token (`google.golang.org/api/idtoken`).
- **Failures aren't cached.** A datacenter IP occasionally gets a bot-challenge page; the
  worker retries once and, if still empty, returns an error instead of caching "no data".
- **No-city chains are anchored with `regionCode=US`** so a location-less query (e.g.
  "AMC Theatres") still resolves a US result from a datacenter IP.
- **No affiliate, by design.** Revelio gives the verdict and has no buy button - an
  honest advisor shouldn't be paid by the platform it critiques.

## Local development

Prereqs: Docker, Go 1.26+, Python 3.13+, Node 22+. Copy `.env.example` to `.env` and
fill in `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, and `GOOGLE_PLACES_API_KEY`
(each stage degrades gracefully if its key is missing).

```bash
# 1. Redis (local) - or point REDIS_URL at an Upstash instance instead
docker compose up -d

# 2. Python worker  →  http://127.0.0.1:8000
cd worker && pip install -r requirements.txt && playwright install chromium
python -m uvicorn main:app --port 8000

# 3. Go gateway     →  http://127.0.0.1:8080
cd api && REDIS_URL="redis://127.0.0.1:6379" \
          WORKER_URL="http://127.0.0.1:8000" go run .

# 4a. Landing page  →  http://127.0.0.1:5173
cd web && npm install && npm run dev

# 4b. Extension     →  build, then load .output/chrome-mv3 unpacked
cd extension && npm install && npm run dev   # (set API_BASE in entrypoints/background.ts to localhost)
```

Quick CLI (no servers, analyzes one URL):

```bash
cd worker && python -m analyzer "https://www.groupon.com/deals/<slug>"
```

## Deployment

- **Worker & gateway** → Cloud Run via `gcloud run deploy --source` (GitHub Actions on
  push to `api/**` and `worker/**`). The worker is `--no-allow-unauthenticated`.
- **Landing page** → Firebase Hosting (GitHub Actions on push to `web/**`).
- **Cache** → Upstash Redis free tier; connection string set as the gateway's `REDIS_URL`.
- **Extension** → `cd extension && npm run build` (or `npm run zip` to package for the
  Chrome Web Store); loaded/published manually, not via CI.

## Repo layout

```
web/         Vue 3 landing page (+ public/privacy.html)
extension/   WXT + Vue 3 Chrome MV3 extension - entrypoints/, components/, utils/
api/         Go (Gin) gateway - internal/{config,store,server,worker}
worker/      Python FastAPI worker - analyzer/{scrape,parse,discount,competitors,reputation,direct,verdict}
docs/        v2 design notes + screenshot
```
