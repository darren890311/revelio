// Page helpers for the content script.

export const isDealPage = () => location.pathname.startsWith('/deals/');

// The current deal's slug from the URL path, e.g. /deals/<slug>?... → <slug>.
export function urlSlug(): string {
  return (location.pathname.split('/deals/')[1] || '').split(/[/?#]/)[0];
}

// The deal slug that a page's JSON-LD ProductGroup describes, or null. Groupon
// dropped __NEXT_DATA__ (it's now an Apollo/GraphQL SPA), but every deal page
// still server-renders a ProductGroup block for SEO whose `url`/`@id` names the
// deal. On an SPA deal→deal navigation the live ProductGroup lags the first-
// loaded deal, so matching its slug to the URL is how we tell fresh from stale.
function jsonLdSlug(doc: Document): string | null {
  const slugFrom = (u: unknown): string | null => {
    if (typeof u !== 'string') return null;
    const m = u.match(/\/deals\/([^/?#]+)/);
    return (m && m[1]) || null;
  };
  for (const s of doc.querySelectorAll('script[type="application/ld+json"]')) {
    if (!s.textContent) continue;
    try {
      const data = JSON.parse(s.textContent);
      const nodes = Array.isArray(data) ? data : [data];
      for (const n of nodes) {
        const t = n?.['@type'];
        if (t === 'ProductGroup' || t === 'Product') {
          const slug = slugFrom(n.url) || slugFrom(n['@id']);
          if (slug) return slug;
        }
      }
    } catch {
      // skip an unparseable block
    }
  }
  return null;
}

// Live page HTML, but only if its JSON-LD ProductGroup already describes the deal
// in the URL (direct load / refresh). The live DOM also carries the rendered
// price tiers (with the strike-through anchor) and "Similar deals" cards, so this
// is the richest source when it's fresh.
export function pageHtmlIfFresh(): string | null {
  return jsonLdSlug(document) === urlSlug() ? document.documentElement.outerHTML : null;
}

// Fresh HTML for the *current* deal, so the worker can always skip Playwright.
//   1. If the live page is already fresh (direct load / refresh), use it.
//   2. Otherwise the user reached this deal by an SPA click from another page,
//      so the live DOM/JSON-LD is stale. Re-fetch the deal URL same-origin (with
//      the user's session) to get the server-rendered HTML for THIS deal — the
//      same thing a manual refresh would load, but without reloading the page.
// Returns null only if we can't confirm a match, in which case the worker falls
// back to its own scrape.
export async function freshDealHtml(): Promise<string | null> {
  const live = pageHtmlIfFresh();
  if (live) return live;
  try {
    const res = await fetch(location.href, { credentials: 'include' });
    if (!res.ok) return null;
    const html = await res.text();
    const doc = new DOMParser().parseFromString(html, 'text/html');
    return jsonLdSlug(doc) === urlSlug() ? html : null;
  } catch {
    return null;
  }
}

// Recommendations lazy-load after hydration; resolve once a card is in the DOM
// (or after a short cap, so a deal with genuinely no similar deals still runs).
export function waitForCards(timeout = 4000): Promise<void> {
  return new Promise((resolve) => {
    if (document.querySelector('a[data-bhd]')) return resolve();
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (document.querySelector('a[data-bhd]') || Date.now() - t0 > timeout) {
        clearInterval(iv);
        resolve();
      }
    }, 200);
  });
}
