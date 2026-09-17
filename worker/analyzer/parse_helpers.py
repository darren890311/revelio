"""Pure helper functions for parse.py - JSON-LD extraction, pricing, address,
highlights/fine-print heuristics, image alt text, script counts, schema catalog.

Kept separate from parse.py so the orchestrator (parse_audit) stays readable.
"""

import json
import re
from typing import Any

from bs4 import BeautifulSoup


# --- text and number coercion ---------------------------------------------

def text(el) -> str | None:
    if el is None:
        return None
    t = el.get_text(" ", strip=True)
    return t or None


def money(s: Any) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"\$?\s*(\d{1,5}(?:[.,]\d{1,2})?)", str(s).replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


# --- JSON-LD ---------------------------------------------------------------

def collect_jsonld(soup: BeautifulSoup) -> list[dict[str, Any]]:
    blocks = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.text or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("@graph"), list):
                blocks.extend(d for d in item["@graph"] if isinstance(d, dict))
            else:
                blocks.append(item)
    return blocks


def find_jsonld(blocks: list[dict[str, Any]], type_name: str) -> dict[str, Any] | None:
    for b in blocks:
        t = b.get("@type")
        if t == type_name or (isinstance(t, list) and type_name in t):
            return b
    return None


def find_business(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    business_types = {
        "HealthAndBeautyBusiness", "BeautySalon", "DaySpa", "AutoRepair",
        "Restaurant", "TouristAttraction", "LocalBusiness", "Store",
        "MedicalBusiness", "Dentist", "Optician", "HairSalon", "Museum",
    }
    for b in blocks:
        t = b.get("@type")
        if t in business_types or (isinstance(t, list) and any(x in business_types for x in t)):
            return b
    return None


def list_schema_types(blocks: list[dict[str, Any]]) -> list[str]:
    types: list[str] = []
    for b in blocks:
        t = b.get("@type")
        if isinstance(t, list):
            types.extend(str(x) for x in t)
        elif t:
            types.append(str(t))
    seen: set[str] = set()
    out: list[str] = []
    for t in types:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# --- breadcrumbs / address / FAQs -----------------------------------------

def extract_breadcrumbs(blocks: list[dict[str, Any]]) -> list[str]:
    bc = find_jsonld(blocks, "BreadcrumbList")
    if not bc or not isinstance(bc.get("itemListElement"), list):
        return []
    crumbs: list[str] = []
    for item in bc["itemListElement"]:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name and isinstance(item.get("item"), dict):
            name = item["item"].get("name")
        if name:
            crumbs.append(name)
    return crumbs


def looks_like_venue_name(s: str) -> bool:
    if not s:
        return False
    venue_words = ("hotel", "mall", "plaza", "center", "centre", "square", "tower", "building")
    sl = s.lower()
    return any(w in sl for w in venue_words)


def extract_address(business: dict[str, Any] | None) -> tuple[str | None, str | None, str | None]:
    if not business:
        return None, None, None
    addr = business.get("address")
    if not isinstance(addr, dict):
        return None, None, None
    street = addr.get("streetAddress")
    locality = addr.get("addressLocality")
    region = addr.get("addressRegion")

    city: str | None = None
    if street and isinstance(street, str):
        m = re.search(r",\s*([A-Z][A-Za-z .'-]{2,40})\s*$", street.strip())
        if m:
            city = m.group(1).strip()
    if not city and locality and not looks_like_venue_name(locality):
        city = locality
    return city, region, street


def extract_faqs(blocks: list[dict[str, Any]]) -> list[dict[str, str]]:
    faq = find_jsonld(blocks, "FAQPage")
    if not faq or not isinstance(faq.get("mainEntity"), list):
        return []
    out = []
    for q in faq["mainEntity"]:
        if not isinstance(q, dict):
            continue
        question = q.get("name")
        ans = q.get("acceptedAnswer") or {}
        answer = ans.get("text") if isinstance(ans, dict) else None
        if question and answer:
            out.append({"question": question, "answer": answer})
    return out[:20]


# --- pricing ---------------------------------------------------------------

def extract_prices_from_variants(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-tier pricing from JSON-LD ProductGroup variants.

    `offers.price` is the standard Groupon price - the deal price we compare on.
    `priceSpecification` is EITHER a ListPrice (higher → the strike-through anchor)
    or a SalePrice (lower → a promo-code price, e.g. "$14.79 with code FALL").
    The promo requires a transient sitewide code, so it is NOT the deal price: we
    keep it aside and never compare on it. The true strike-through anchor is often
    absent from JSON-LD (only the promo SalePrice is given), so `original_price`
    may come back None here. This is only a FALLBACK for the rare path with no
    rendered DOM; extract_prices_from_dom() is the primary, reliable source.
    """
    out = []
    for v in variants:
        if not isinstance(v, dict):
            continue
        offer = v.get("offers")
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        if not isinstance(offer, dict):
            continue
        groupon_price = money(offer.get("price"))
        original_price = None
        promo_price = None
        spec = offer.get("priceSpecification")
        if isinstance(spec, dict):
            sp = money(spec.get("price"))
            if sp is not None and groupon_price is not None:
                price_type = (spec.get("priceType") or "").lower()
                if "listprice" in price_type or sp > groupon_price:
                    original_price = sp  # a real strike-through anchor
                elif "saleprice" in price_type or sp < groupon_price:
                    promo_price = sp  # promo-code price; kept aside, not compared on
        discount_pct = None
        if original_price and groupon_price and original_price > 0 and groupon_price < original_price:
            discount_pct = round((1 - groupon_price / original_price) * 100, 1)
        out.append({
            "label": v.get("name") or "Default",
            "original_price": original_price,
            "deal_price": groupon_price,
            "discount_pct": discount_pct,
            "promo_price": promo_price,
        })
    return out


def extract_prices_from_dom(soup: BeautifulSoup) -> list[dict[str, float]]:
    """Authoritative per-tier pricing straight off the rendered DOM.

    Groupon's SPA no longer embeds trustworthy prices in JSON-LD: on a promo deal
    the JSON-LD `offers.price`/`priceSpecification` carry the (lower) Groupon and
    promo-code prices in inconsistent roles, and the TRUE strike-through anchor
    ($258 on a "$258 / $85.14 -67% / $63.86 with code" tier) never appears there
    at all - it renders only in the DOM. Groupon tags each price with a stable
    `data-testid`, so we read exactly what the shopper sees:

        <span data-testid="strike-through-price">$258</span>   ← original
        <span data-testid="green-price">$85.14</span>          ← deal price
        <span data-testid="discount">-67%</span>               ← (badge, unused)
        ... "$63.86 with code RELAX" lives elsewhere            ← promo, ignored

    The deal price is the `green-price`; the original is the `strike-through-price`
    in the same price block. We skip anything inside an `a[data-bhd]` competitor
    tile, and dedupe by (original, deal) so a tier rendered twice (selected card +
    list row) collapses to one. The "with code" promo is deliberately not read -
    we never compare on it.
    """
    out: list[dict[str, float]] = []
    seen: set[tuple[float, float]] = set()
    for green in soup.select('[data-testid="green-price"]'):
        if green.find_parent("a", attrs={"data-bhd": True}) is not None:
            continue  # a "Similar deals" competitor tile, not this deal
        deal = money(green.get_text())
        if deal is None or deal <= 0:
            continue

        # The strike-through anchor sits in the same price block; climb to the
        # smallest ancestor that contains one so a neighbouring tier can't leak in.
        strikes = []
        block = green
        for _ in range(4):
            block = block.parent
            if block is None:
                break
            strikes = block.select('[data-testid="strike-through-price"]')
            if strikes:
                break
        # A tier may render an intermediate struck price too (e.g. $450 then
        # $292.50); the true regular-price anchor is the highest of them.
        vals = [v for v in (money(s.get_text()) for s in strikes) if v is not None and v > 0]
        if not vals:
            continue
        original = max(vals)
        if original <= deal:
            continue

        key = (round(original, 2), round(deal, 2))
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "original_price": original,
            "deal_price": deal,
            "discount_pct": round((1 - deal / original) * 100, 1),
        })
    return out


def variant_price_labels(variants: list[dict[str, Any]]) -> dict[float, str]:
    """Map every price a JSON-LD variant mentions (its `offers.price` and its
    `priceSpecification.price`) to that variant's option label. A DOM tier's deal
    price always equals one of these, so this joins the DOM's (reliable) prices
    back to the (reliable) JSON-LD labels without trusting JSON-LD's price roles."""
    out: dict[float, str] = {}
    for v in variants:
        if not isinstance(v, dict):
            continue
        label = v.get("name")
        if not label:
            continue
        offer = v.get("offers")
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        if not isinstance(offer, dict):
            continue
        candidates = [offer.get("price")]
        spec = offer.get("priceSpecification")
        if isinstance(spec, dict):
            candidates.append(spec.get("price"))
        for c in candidates:
            val = money(c)
            if val is not None:
                out[round(val, 2)] = label
    return out


def _nd_amount(price_obj: Any) -> float | None:
    """Groupon Next.js prices are {amount: <cents>, ...}."""
    if isinstance(price_obj, dict):
        amt = price_obj.get("amount")
        if isinstance(amt, (int, float)):
            return amt / 100
    return None


def extract_prices_from_next_data(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Authoritative deal-page pricing from the embedded Next.js state.

    Each `DealOption` carries `unformattedStrikeThroughPrice` (the real anchor)
    and `unformattedPrice` (the deal price) - what the page actually renders.
    The JSON-LD `offers`, by contrast, on promo-code deals encode the deal price
    as `price` and the promo price as `SalePrice`, hiding the true anchor and
    yielding a wrong discount (e.g. 25% instead of the real 50%).
    """
    nd = soup.find("script", id="__NEXT_DATA__")
    if not nd or not nd.string:
        return []
    try:
        data = json.loads(nd.string)
    except json.JSONDecodeError:
        return []
    apollo = (((data.get("props") or {}).get("pageProps") or {}).get("__APOLLO_STATE__")) or {}
    if not isinstance(apollo, dict):
        return []

    out: list[dict[str, Any]] = []
    for key, obj in apollo.items():
        if not key.startswith("DealOption:") or not isinstance(obj, dict):
            continue
        deal_price = _nd_amount(obj.get("unformattedPrice"))
        if deal_price is None:
            continue
        strike = _nd_amount(obj.get("unformattedStrikeThroughPrice"))
        original = strike if strike is not None else deal_price
        discount_pct = None
        if original and original > 0 and deal_price < original:
            discount_pct = round((1 - deal_price / original) * 100, 1)
        out.append({
            "label": obj.get("title") or "Default",
            "original_price": original,
            "deal_price": deal_price,
            "discount_pct": discount_pct,
        })
    return out


# --- DOM-based extractors (fallbacks for content not in JSON-LD) -----------

def is_content_list(ul) -> bool:
    """Reject obvious nav/breadcrumb/tab lists."""
    text_attr = (ul.get("class") and " ".join(ul.get("class")).lower()) or ""
    if any(bad in text_attr for bad in ("nav", "breadcrumb", "tab", "menu", "header", "footer")):
        return False
    parent = ul.parent
    while parent is not None and getattr(parent, "name", None):
        cls = parent.get("class") if hasattr(parent, "get") else None
        if cls:
            joined = " ".join(cls).lower()
            if any(bad in joined for bad in ("nav", "header", "footer", "tab")):
                return False
        if parent.name in ("nav", "header", "footer"):
            return False
        parent = parent.parent
    return True


def extract_highlights(soup: BeautifulSoup) -> list[str]:
    candidates: list[str] = []
    for header in soup.find_all(["h2", "h3", "h4"]):
        label = text(header) or ""
        if re.search(r"highlight|what you get|what's included|the deal", label, re.IGNORECASE):
            ul = header.find_next("ul")
            if ul and is_content_list(ul):
                items = [text(li) for li in ul.find_all("li")]
                items = [t for t in items if t]
                if items:
                    candidates.extend(items)
                    break
    if not candidates:
        for ul in soup.find_all("ul"):
            if not is_content_list(ul):
                continue
            items = [text(li) for li in ul.find_all("li")]
            items = [t for t in items if t and 8 < len(t) < 250]
            if 2 <= len(items) <= 12:
                candidates = items
                break
    return candidates[:15]


def extract_fine_print(soup: BeautifulSoup) -> str | None:
    for header in soup.find_all(["h2", "h3", "h4"]):
        label = text(header) or ""
        if re.search(r"fine print|terms|conditions|need to know", label, re.IGNORECASE):
            sib = header.find_next_sibling()
            chunks: list[str] = []
            for _ in range(5):
                if sib is None:
                    break
                t = text(sib)
                if t:
                    chunks.append(t)
                sib = sib.find_next_sibling()
            if chunks:
                return " ".join(chunks)[:3000]
    return None


# --- images / scripts ------------------------------------------------------

def extract_alt_text(soup: BeautifulSoup, sample_n: int = 20) -> tuple[dict, list[dict]]:
    imgs = soup.find_all("img")
    real = [img for img in imgs if img.get("src") or img.get("data-src")]
    with_alt = [img for img in real if (img.get("alt") or "").strip()]
    sample = []
    for img in with_alt[:sample_n]:
        src = img.get("src") or img.get("data-src") or ""
        alt = (img.get("alt") or "").strip()
        sample.append({"src": src[:240], "alt": alt[:240]})
    coverage = {
        "total_images": len(real),
        "with_alt_text": len(with_alt),
        "coverage_pct": round(100 * len(with_alt) / len(real), 1) if real else 0.0,
    }
    return coverage, sample


def count_scripts(soup: BeautifulSoup) -> dict[str, int]:
    scripts = soup.find_all("script")
    return {
        "total": len(scripts),
        "external": sum(1 for s in scripts if s.get("src")),
        "inline": sum(1 for s in scripts if not s.get("src")),
        "json_ld": sum(1 for s in scripts if s.get("type") == "application/ld+json"),
    }
