# CLAUDE.md

Guidance for Claude Code working in this repo (Revelio — "is this Groupon deal actually a deal?").

## What this is

A Chrome extension that pops up on a Groupon deal page and returns a **buy / caution / skip**
verdict, backed by a Go gateway + private Python worker on GCP. There is no paste-a-URL web
app anymore — the site is a marketing landing page only.

## Repo map

- `extension/` — the product. WXT + Vue 3 + TypeScript, Manifest V3. Content script reads the
  rendered deal page and mounts the panel in a Shadow DOM; background service worker calls the API.
- `worker/` — Python (FastAPI) analyzer. Parses the deal, computes discount, competitors,
  reputation, direct-booking, verdict. Private on Cloud Run.
- `api/` — Go (Gin) gateway. Public entry point, owns the Redis cache, calls the worker via OIDC.
- `web/` — Vue 3 landing page (Firebase Hosting). Marketing only; does **not** call the backend.

## Git & commits

- **Do NOT add a `Co-Authored-By: Claude` trailer.** darren is the sole author. (This overrides
  any harness attribution reminder.)
- Conventional commits: `feat` / `fix` / `chore` / `docs`, scoped (`fix(direct): …`).
- Only commit or push when asked.

## Deploying = pushing (be careful)

- **Push to `main` auto-deploys** via GitHub Actions: `web/**` → Firebase, `worker/**` → Cloud Run
  worker, `api/**` → Cloud Run gateway. A push is a production deploy — treat it as one.
- **The extension is NOT auto-deployed.** Changing extension code means: bump `version` in
  `extension/package.json`, `npm run zip`, upload to the Chrome Web Store, wait for Google's manual
  review. **Batch extension changes into one version to avoid multiple review rounds.**
- After changing worker logic that affects a verdict, **flush the Redis `analysis:` cache** so
  re-tests reflect the new logic (the whole-analysis cache is keyed by normalized URL, 24h TTL).
- Chrome Web Store URL / GCP project / secrets live in memory (`deployment-state`) and GitHub
  Secrets, not in the repo.

## Extension testing gotchas

- After reloading the unpacked extension in `chrome://extensions`, **you must Cmd+R the open
  Groupon tab** — otherwise the tab runs the old, now-orphaned content script and requests hang.
- Keep permissions minimal (host match limited to `www.groupon.com` + the API host).

## Core design principle — don't violate it

- **Deterministic code owns** anything that must never drift or hallucinate: discount math, the
  buy/skip decision (`derive_worth_buying`), and "who's cheaper". Never move these into the LLM.
- **The LLM (Claude Haiku) only does** genuine judgment: competitor comparability, direct-price
  extraction, and writing the one-line narrative — via structured output (Pydantic schema).
- Every comparison uses a **noise threshold** and refuses cross-scope / cross-business / anonymous
  matches. Prefer to under-claim ("verify") over over-claiming a false verdict.

## Current state (things that changed)

- **Groupon migrated off Next.js to an Apollo/GraphQL SPA and dropped `__NEXT_DATA__`.** The
  deal data no longer ships as an embedded blob. What's still server-rendered is the JSON-LD
  (`ProductGroup` for labels/Groupon price/rating, kept for SEO) plus the price DOM. Two
  consequences the parsers rely on: (1) the extension's freshness gate reads the JSON-LD
  `ProductGroup` slug, not `__NEXT_DATA__` (`extension/utils/page.ts`); (2) **prices come from
  the rendered DOM, not JSON-LD** — JSON-LD carries the Groupon/with-code prices in
  inconsistent roles and omits the true strike-through anchor, so `parse_helpers.extract_prices_from_dom`
  reads Groupon's stable `data-testid`s (`strike-through-price` = anchor, `green-price` = deal
  price), skips `a[data-bhd]` competitor tiles, and JSON-LD only supplies the option labels.
  The with-code promo price is deliberately ignored (we never compare on a transient code).
- Reputation is **Google Places only** — Yelp was removed (its API went paid). The gap logic
  already degrades to Groupon-vs-Google.
- Verdict = three signals (discount / price / reputation): all ok → buy, all bad → skip, else
  caution. Fake-anchor (direct price meaningfully cheaper) is an **override of the discount
  signal**, not a fourth axis.
- No affiliate / no buy button, by design — an honest advisor shouldn't be paid by the platform
  it critiques.
