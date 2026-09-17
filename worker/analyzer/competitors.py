"""Competitor discovery - same city, on Groupon, with service comparability.

Strategy (hybrid):
  1. Tavily, constrained to groupon.com, to discover the Groupon *local category*
     page URL for this deal's (category, city) - e.g. /local/chicago/oil-change.
  2. Playwright-scrape that page. It is already city-scoped, so every deal on it
     is a same-city competitor by construction.
  3. BeautifulSoup locates the page's embedded Apollo state
     (__NEXT_DATA__ → __APOLLO_STATE__.ROOT_QUERY.browseDealFeed(...).cards),
     which carries real prices + discounts (no fragile DOM scraping).
  4. Claude (Sonnet) judges how comparable each competitor's *service* is to the
     analyzed deal - "same" / "similar" / "different" + a one-line difference - 
     so a full-synthetic oil change is not blindly compared to a conventional one.

The deal under analysis is excluded by slug; remaining competitors are labelled,
marked `cheaper`, and returned with like-for-like ("same") matches first.
"""

import json
import re
from pathlib import Path

from . import cache
from typing import Any, Literal
from urllib.parse import urlparse

import anthropic
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from . import scrape
from .config import HAIKU_MODEL
from .models import Competitor

_MATCH_RANK = {"same": 0, "similar": 1, None: 1, "different": 2}


# --- parse the local category page (pure, network-free) --------------------

def _amount(price_obj: Any) -> int | None:
    """Apollo prices are objects with an integer `amount` in cents."""
    if isinstance(price_obj, dict):
        amt = price_obj.get("amount")
        return amt if isinstance(amt, (int, float)) else None
    return None


def _norm_merchant(s: str | None) -> str:
    """Strip decoration/punctuation/case so 'JCPenney Portraits by Lifetouch' and
    '- * JCPenney Portraits by Lifetouch * -' compare equal."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _same_merchant(a: str | None, b: str | None) -> bool:
    """Whether two cards are the SAME business. A merchant's *other* deal isn't a
    competitor - comparing a studio's full-package deal to its own one-pose deal
    and calling it 'pricier than comparable' is nonsense."""
    na, nb = _norm_merchant(a), _norm_merchant(b)
    if not na or not nb:
        return False
    return na == nb or (len(na) >= 5 and len(nb) >= 5 and (na in nb or nb in na))


def parse_local_cards(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    nd = soup.find("script", id="__NEXT_DATA__")
    if not nd or not nd.string:
        return []
    try:
        data = json.loads(nd.string)
    except json.JSONDecodeError:
        return []

    root = (
        ((data.get("props") or {}).get("pageProps") or {})
        .get("__APOLLO_STATE__", {})
        .get("ROOT_QUERY", {})
    )
    cards: list[Any] = []
    for k, v in root.items():
        if k.startswith("browseDealFeed(") and isinstance(v, dict):
            cards = v.get("cards") or []
            break

    out: list[dict[str, Any]] = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        prices = c.get("prices") or {}
        deal_cents = _amount(prices.get("price"))
        strike_cents = _amount(prices.get("strikeThroughPrice"))
        merchant = c.get("merchant")
        merchant_name = merchant.get("name") if isinstance(merchant, dict) else None
        rating = c.get("rating") if isinstance(c.get("rating"), dict) else {}
        out.append({
            "slug": c.get("id"),
            "url": c.get("url"),
            "title": c.get("title"),
            "merchant": merchant_name,
            "deal_price": deal_cents / 100 if deal_cents is not None else None,
            "original_price": strike_cents / 100 if strike_cents is not None else None,
            "discount_pct": c.get("discountPercentage"),
            "rating": rating.get("value"),
            "rating_count": rating.get("count"),
        })
    return out


def parse_similar_cards(html: str) -> list[dict[str, Any]]:
    """Competitor cards straight off a *deal* page's on-page recommendations.

    Each "Similar/Recommended deal" renders as an `<a data-bhd="...">` whose
    attribute holds a JSON blob with typed sections (title / prices / subtitle /
    rating). Prices are integer cents. This is the extension path: the cards are
    already in the rendered HTML the content script sends, so no separate
    category-page scrape (and no Playwright) is needed.
    """
    soup = BeautifulSoup(html, "lxml")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for a in soup.select("a[data-bhd]"):
        try:
            bhd = json.loads(a.get("data-bhd") or "")
        except (json.JSONDecodeError, TypeError):
            continue

        by_type: dict[str, Any] = {}
        for sec in (bhd.get("body") or {}).values():
            if isinstance(sec, dict) and sec.get("type"):
                by_type[sec["type"]] = sec.get("content")

        prices = by_type.get("prices")
        if not isinstance(prices, dict):
            continue  # not a deal card (could be a nav/category link)

        href = a.get("href") or ""
        slug = scrape.slug_from_url(href) if href else None
        if not slug or slug in seen:
            continue
        seen.add(slug)

        sell = prices.get("sell_price")
        listp = prices.get("list_price")
        rating = by_type.get("rating") if isinstance(by_type.get("rating"), dict) else {}
        out.append({
            "slug": slug,
            "url": "https://www.groupon.com" + href if href.startswith("/") else href,
            "title": by_type.get("title1") or by_type.get("title"),
            "merchant": by_type.get("merchant"),  # cards often omit it; title carries the signal
            "deal_price": sell / 100 if isinstance(sell, (int, float)) else None,
            "original_price": listp / 100 if isinstance(listp, (int, float)) else None,
            "discount_pct": prices.get("discount"),
            "rating": rating.get("numeric_value"),
            # Groupon's SPA renamed the count field to "count"; keep the old name
            # as a fallback for older cached cards.
            "rating_count": rating.get("count") or rating.get("number_of_ratings"),
        })
    return out


# --- discover the local category URL via Tavily ----------------------------

def _category_leaf(category: str | None) -> str:
    """The breadcrumb leaf ('Local > ... > Oil Change' → 'Oil Change')."""
    if not category:
        return ""
    return category.split(">")[-1].strip()


def discover_local_url(category: str | None, city: str | None, tavily_client) -> str | None:
    """Find the best groupon.com/local/<city>/<category> URL for this deal."""
    leaf = _category_leaf(category)
    query = " ".join(p for p in (leaf, city, "Groupon") if p).strip()
    if not query or tavily_client is None:
        return None

    try:
        res = tavily_client.search(
            query=query,
            include_domains=["groupon.com"],
            max_results=8,
            search_depth="basic",
        )
    except Exception as e:
        print(f"  Tavily discover error: {e}")
        return None

    city_token = (city or "").lower().split(",")[0].strip().replace(" ", "-")
    local_urls = [
        r.get("url") for r in res.get("results", [])
        if r.get("url") and "/local/" in urlparse(r["url"]).path
    ]
    if not local_urls:
        return None
    # Prefer a URL whose path mentions this city.
    for u in local_urls:
        if city_token and city_token in urlparse(u).path.lower():
            return u
    return local_urls[0]


# --- LLM service comparability ---------------------------------------------

class _ComparabilityItem(BaseModel):
    index: int
    match: Literal["same", "similar", "different"]
    difference_note: str = Field(
        default="", description="<=12 words naming the key spec difference; empty when match is 'same'"
    )


class _ComparabilityResult(BaseModel):
    items: list[_ComparabilityItem]


def classify_comparability(
    client: anthropic.Anthropic | None,
    deal_title: str | None,
    deal_options: list[str] | None,
    competitors: list[Competitor],
) -> None:
    """Annotate each competitor in place with `match` + `difference_note`.

    No-op when no client/competitors. On any failure, leaves match=None so the
    pipeline degrades gracefully (treated as "similar" for ranking).
    """
    if not client or not competitors:
        return

    options_str = "; ".join(deal_options) if deal_options else "(none listed)"
    comp_block = "\n".join(
        f"{i}. {c.title or '(untitled)'}  [merchant: {c.merchant}]"
        for i, c in enumerate(competitors)
    )
    prompt = f"""The shopper is looking at THIS Groupon deal:
- Title: {deal_title}
- Service options: {options_str}

Below are competing deals in the same city and category. For each, classify how comparable its service is to the shopper's deal:
- "same": an equivalent substitute - the same service tier/spec/scope the shopper would accept with no trade-off.
- "similar": the SAME kind of service/experience, with a meaningful spec or scope difference (e.g. conventional/blend vs full synthetic oil, missing tire rotation, partial vs full highlights, 50-min vs 60-min massage, a 3-attraction pass vs a 5-attraction pass, 3 sessions vs 6 sessions).
- "different": a different kind of service, venue, or experience - NOT a real substitute, even if it sits in the same Groupon category.

Be strict on three things:
1. full synthetic vs conventional/blend is at most "similar", never "same".
2. A different kind of venue/experience is "different", not "similar" - e.g. a waterpark vs a county fair, a museum vs a zoo, a massage vs a facial. Only mark "similar" when a shopper who wants THIS deal would seriously consider it as a near-substitute.
3. A different QUANTITY, COUNT, COVERAGE or package scope (number of attractions/sessions/visits/days, duration, area covered, items included) is a meaningful difference → at most "similar", never "same" - EVEN when the brand or product name matches. E.g. a "CityPASS C3 / choice of 3 attractions" is only "similar" to a "CityPASS / 5 attractions"; a 6-session package is only "similar" to a 3-session one. Compare what is actually included, not the brand.

For "similar"/"different", give a difference_note of <=12 words naming the key difference. For "same", leave difference_note empty.

Competitors:
{comp_block}"""

    try:
        resp = client.messages.parse(
            model=HAIKU_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            output_format=_ComparabilityResult,
        )
        by_idx = {it.index: it for it in resp.parsed_output.items}
    except Exception as e:
        print(f"  comparability classification failed: {e}")
        return

    for i, c in enumerate(competitors):
        it = by_idx.get(i)
        if it:
            c.match = it.match
            c.difference_note = it.difference_note


def like_for_like_range(competitors: list[Competitor]) -> tuple[float, float] | None:
    """Min/max deal price across "same"-service competitors - the fair benchmark."""
    prices = [c.price for c in competitors if c.match == "same" and c.price is not None]
    if not prices:
        return None
    return min(prices), max(prices)


# --- orchestrator ----------------------------------------------------------

def find_competitors(
    category: str | None,
    city: str | None,
    *,
    exclude_slug: str | None,
    deal_price: float | None,
    deal_title: str | None = None,
    deal_merchant: str | None = None,
    deal_options: list[str] | None = None,
    tavily_client,
    anthropic_client: anthropic.Anthropic | None = None,
    max_n: int = 5,
    cache_dir: str | Path | None = None,
    prefetched_cards: list[dict[str, Any]] | None = None,
) -> list[Competitor]:
    # Extension path: the deal page's on-page "Similar deals" were already parsed
    # from the HTML the content script sent - use them directly, no scrape.
    if prefetched_cards is not None:
        cards = prefetched_cards
    else:
        # The slow fallback (Tavily discovery + Playwright scrape of the category
        # page) depends only on category + city, not the specific deal, so cache
        # the raw cards and let every deal in that category reuse them. Per-deal
        # comparability/filtering still runs fresh below.
        ckey = f"compcards:{cache.norm(category)}|{cache.norm(city)}"
        cards = cache.get(ckey)
        if cards is None:
            local_url = discover_local_url(category, city, tavily_client)
            if not local_url:
                return []
            try:
                html = scrape.fetch_html(local_url, cache_dir=cache_dir)
                cards = parse_local_cards(html)
                # A datacenter IP occasionally gets a bot-challenge/empty page, which
                # parses to zero cards and would wrongly read as "no comparable deals".
                # Retry once before giving up - a fresh fetch usually gets through.
                if not cards:
                    html = scrape.fetch_html(local_url, cache_dir=cache_dir, force=True)
                    cards = parse_local_cards(html)
            except Exception as e:
                print(f"  competitor page scrape failed ({local_url}): {e}")
                return []
            if cards:  # don't cache an empty/bot-challenged result
                cache.put(ckey, cards, ttl=21600)

    competitors: list[Competitor] = []
    for c in cards:
        if exclude_slug and c.get("slug") == exclude_slug:
            continue
        # The same merchant's other deal is not a competitor - skip it so a
        # studio's own one-pose package can't make its full-shoot deal look
        # "pricier than comparable."
        if _same_merchant(c.get("merchant"), deal_merchant):
            continue
        # A missing or non-positive price means a coupon / "online sale" card with
        # no real Groupon price (e.g. a Sam's Club membership offer that links to a
        # retailer coupon page), not a priced deal we can compare. Skip it so it
        # can't surface as a bogus "$0 cheaper" entry.
        if c.get("deal_price") is None or c["deal_price"] <= 0:
            continue
        competitors.append(Competitor(
            merchant=c.get("merchant"),
            title=c.get("title"),
            price=c.get("deal_price"),
            discount_pct=c.get("discount_pct"),
            url=c.get("url"),
            cheaper=deal_price is not None and c["deal_price"] < deal_price,
        ))

    # Judge service comparability across all candidates before trimming, so
    # like-for-like matches surface even if a different-service deal is cheaper.
    classify_comparability(anthropic_client, deal_title, deal_options, competitors)

    # Drop deals that are a different service entirely (e.g. a brake job vs an oil
    # change) - they aren't real alternatives, just same-category noise. Keep None
    # (unclassified) so the degraded no-LLM path still returns something.
    competitors = [c for c in competitors if c.match != "different"]

    # Like-for-like ("same") first, then by price ascending.
    competitors.sort(key=lambda x: (_MATCH_RANK.get(x.match, 1), x.price is None, x.price))
    return competitors[:max_n]


# --- standalone demo: python -m analyzer.competitors <deal_url> -------------

def _demo() -> int:
    import os
    import sys

    from tavily import TavilyClient

    from .config import TAVILY_API_KEY
    from .pipeline import analyze

    if len(sys.argv) < 2:
        print("usage: python -m analyzer.competitors <groupon-deal-url>", file=sys.stderr)
        return 2

    deal_url = sys.argv[1]
    cache_dir = Path(__file__).resolve().parents[1] / "explore_cache"

    # Analyze the deal first to derive city/category/price/service from the page.
    analysis = analyze(deal_url, cache_dir=Path(__file__).resolve().parents[2] / "data" / "raw_html")
    d = analysis.deal
    deal_price = min((t.deal for t in d.prices if t.deal is not None), default=None)
    options = [t.label for t in d.prices if t.label]

    print(f"Deal: {d.title}  ({d.city}, {d.category})")
    print(f"  price={deal_price}  options={options}\n")

    tavily = TavilyClient(api_key=TAVILY_API_KEY or os.environ["TAVILY_API_KEY"])
    client = anthropic.Anthropic()

    comps = find_competitors(
        d.category, d.city,
        exclude_slug=scrape.slug_from_url(deal_url),
        deal_price=deal_price,
        deal_title=d.title, deal_options=options,
        tavily_client=tavily, anthropic_client=client,
        cache_dir=cache_dir,
    )
    print(json.dumps([c.model_dump() for c in comps], ensure_ascii=False, indent=2))
    rng = like_for_like_range(comps)
    print(f"\nlike-for-like (same-service) price range: {rng}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
