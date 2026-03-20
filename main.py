import requests
import os
import logging
import re

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("rebel_checker.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SEARCH_URL = (
    "https://fromrebel.com/search?q=avi+jogging+stroller"
    "&filter.p.product_type=Cybex"
)

# Rebel runs on Shopify — we can hit the JSON endpoint directly (no JS needed)
# The search page with .json suffix returns structured product data.
SEARCH_JSON_URL = (
    "https://fromrebel.com/search?q=avi+jogging+stroller"
    "&filter.p.product_type=Cybex"
    "&view=json"          # many Shopify stores expose this; we fall back if not
)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── Discord ───────────────────────────────────────────────────────────────────

def send_discord_alert(available_products: list[dict]) -> None:
    """Send a Discord notification listing all newly-available products."""
    if not DISCORD_WEBHOOK:
        logger.error("DISCORD_WEBHOOK env var not set — cannot send alert.")
        return

    lines = []
    for p in available_products:
        price_str = f"${p['price']:.2f}" if p.get("price") else "Price N/A"
        lines.append(f"• **{p['title']}** — {price_str}\n  {p['url']}")

    body = "\n\n".join(lines)
    payload = {
        "content": (
            f"🛒 **Cybex AVI Jogging Stroller NOW AVAILABLE at Rebel!**\n\n"
            f"{body}\n\n"
            f"🔗 Search page: {SEARCH_URL}"
        )
    }

    try:
        resp = requests.post(DISCORD_WEBHOOK, json=payload, timeout=30)
        resp.raise_for_status()
        logger.info("Discord alert sent successfully.")
    except requests.RequestException as e:
        logger.error(f"Failed to send Discord alert: {e}")


# ── Scraping helpers ──────────────────────────────────────────────────────────

def fetch_html(url: str) -> str | None:
    """Fetch raw HTML from a URL, returning None on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        logger.info(f"Fetched {url} — status {resp.status_code}")
        return resp.text
    except requests.RequestException as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return None


def parse_products_from_html(html: str) -> list[dict]:
    """
    Parse product cards from the Rebel search results page (plain HTML).

    Shopify search pages typically render product cards that contain:
      - A product title inside an <a> or heading tag
      - A price inside a span with 'price' in its class
      - An "Add to cart" or "Sold out" / "View product" button

    We look for those patterns with regex since we are not using BeautifulSoup
    to keep dependencies minimal (requests only).
    """
    products = []

    # Each product card is wrapped in an <li> with class containing 'product'
    # or a <div class="...product-item...">. We split on common Shopify card boundaries.
    # Strategy: find all product card blocks then extract fields from each.

    card_pattern = re.compile(
        r'(?:<li[^>]*class="[^"]*(?:product|grid__item)[^"]*"[^>]*>|'
        r'<div[^>]*class="[^"]*(?:product-item|card-wrapper)[^"]*"[^>]*>)'
        r'(.*?)'
        r'(?:</li>|</div>\s*</div>)',
        re.DOTALL | re.IGNORECASE,
    )

    # Fallback: look for <article> tags (some Shopify themes use these)
    if not card_pattern.search(html):
        card_pattern = re.compile(
            r'<article[^>]*>(.*?)</article>',
            re.DOTALL | re.IGNORECASE,
        )

    for match in card_pattern.finditer(html):
        block = match.group(1)

        # ── Availability check ──────────────────────────────────────────────
        # We're interested in blocks that contain "Add to cart" or "View product"
        # (case-insensitive). "Sold out" blocks are skipped.
        block_lower = block.lower()
        is_available = (
            "add to cart" in block_lower or "view product" in block_lower
        )
        if not is_available:
            logger.debug("Skipping unavailable product card.")
            continue

        # ── Title ───────────────────────────────────────────────────────────
        title = "Unknown Product"
        title_match = re.search(
            r'(?:class="[^"]*(?:card__heading|product[_-]title|product[_-]name)[^"]*"[^>]*>|'
            r'<h[23][^>]*>)\s*<a[^>]*>([^<]+)</a>',
            block, re.IGNORECASE,
        )
        if not title_match:
            # Generic fallback: first <a> with meaningful text
            title_match = re.search(r'<a[^>]+href="/products/[^"]+">([^<]{5,})</a>', block, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()

        # ── URL ─────────────────────────────────────────────────────────────
        url = SEARCH_URL
        url_match = re.search(r'href="(/products/[^"?]+)"', block, re.IGNORECASE)
        if url_match:
            url = "https://fromrebel.com" + url_match.group(1)

        # ── Price ───────────────────────────────────────────────────────────
        price = None
        price_match = re.search(
            r'(?:class="[^"]*price[^"]*"[^>]*>)[^<$]*\$\s*([\d,]+\.?\d*)',
            block, re.IGNORECASE,
        )
        if not price_match:
            price_match = re.search(r'\$\s*([\d,]+\.?\d{2})', block)
        if price_match:
            try:
                price = float(price_match.group(1).replace(",", ""))
            except ValueError:
                pass

        products.append({"title": title, "url": url, "price": price})
        logger.info(f"Available product found: {title} — ${price} — {url}")

    return products


# ── Main ──────────────────────────────────────────────────────────────────────

def check_rebel() -> None:
    logger.info("=== Rebel Stroller Checker starting ===")

    html = fetch_html(SEARCH_URL)
    if not html:
        logger.error("Could not fetch the search page. Aborting.")
        return

    # Save raw HTML for debugging (useful when running in GitHub Actions)
    with open("rebel_debug.html", "w", encoding="utf-8") as f:
        f.write(html)
    logger.info("Raw HTML saved to rebel_debug.html")

    available = parse_products_from_html(html)

    if available:
        logger.info(f"🛒 {len(available)} available product(s) found — sending alert.")
        send_discord_alert(available)
    else:
        logger.info("No available products found (all sold out or none listed).")

    logger.info("=== Check complete ===")


if __name__ == "__main__":
    check_rebel()
