"""Which stored addresses may be returned as a product URL.

A product URL is a seller product page recorded in catalog.json.
Brand listings, price-tracker pages, and unrecognized addresses are not.
Nothing here invents an address.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

PRODUCT = "product"
BRAND_LISTING = "brand_listing"
PRICE_TRACKER = "price_tracker"
MISSING = "missing"
UNCONFIRMED = "unconfirmed"

_CATALOG_REASONS = {
    BRAND_LISTING: "카탈로그에 저장된 주소는 브랜드 목록 페이지라 상품 URL로 쓰지 않음.",
    PRICE_TRACKER: "카탈로그에 저장된 주소는 가격 추적 페이지라 상품 URL로 쓰지 않음.",
    MISSING: "카탈로그에 주소가 없음.",
    UNCONFIRMED: "저장된 주소를 판매 상품 페이지로 확인하지 못해 URL을 비움.",
}


def classify_stored_url(url: str | None) -> str:
    """Classify a catalog address. Only seller product pages are PRODUCT."""
    raw = (url or "").strip()
    if not raw:
        return MISSING
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    query = (parsed.query or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return UNCONFIRMED
    if host.endswith("lowchart.com") or host.endswith("funiz.com"):
        return PRICE_TRACKER
    if "brand-shop" in path or "brand-shop" in query or "brandname=" in query:
        return BRAND_LISTING
    if _is_search_page(parsed):
        return BRAND_LISTING
    if host.endswith("coupang.com") and _digits_after(path, "/vp/products/"):
        return PRODUCT
    if host.endswith("fitpetmall.com") and _digits_after(path, "/mall/products/"):
        return PRODUCT
    return UNCONFIRMED


def catalog_url_lines(url: str | None) -> list[str]:
    """Lines for a static catalog hit. The URL is printed only for a product page."""
    if classify_stored_url(url) == PRODUCT:
        return [
            f"   상품 URL: {str(url).strip()}",
            "   URL 근거: static_catalog url. 이 호출에서 페이지를 다시 열지 않음.",
        ]
    reason = _CATALOG_REASONS[classify_stored_url(url)]
    return [f"   상품 URL: 없음. {reason}"]


def remote_url_lines(url: str | None) -> list[str]:
    """Lines for a link field returned by the Naver Shopping API.

    Search-result, brand-listing, and price-tracker pages are not product URLs.
    Any other http(s) link is passed through as that API's own link field.
    """
    raw = (url or "").strip()
    if not raw:
        return ["   상품 URL: 없음. 응답에 주소가 없음."]
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ["   상품 URL: 없음. 응답 주소가 웹 주소가 아님."]
    kind = classify_stored_url(raw)
    if kind in {BRAND_LISTING, PRICE_TRACKER} or _is_search_page(parsed):
        return ["   상품 URL: 없음. 검색·목록·가격추적 페이지는 상품 URL로 쓰지 않음."]
    return [
        f"   상품 URL: {raw}",
        "   URL 근거: naver_shopping_api link. 이 호출에서 페이지를 다시 열지 않음.",
    ]


def canonical_url(url: str | None) -> str | None:
    """Compare product URLs without a partner_id tag or a trailing slash."""
    raw = (url or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    pairs = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key != "partner_id"]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse(("https", parsed.hostname.lower(), path, "", urlencode(pairs), ""))


def find_product_by_url(products, url: str | None) -> dict | None:
    """Return the catalog row whose stored url is this seller product page."""
    wanted = canonical_url(url)
    if not wanted:
        return None
    for product in products:
        stored = product.get("url")
        if classify_stored_url(stored) != PRODUCT:
            continue
        if canonical_url(stored) == wanted:
            return product
    return None


def unavailable_reason(url: str | None) -> str:
    """English reason used when get_buy_link will not return an address."""
    kind = classify_stored_url(url)
    if kind == BRAND_LISTING:
        return "The address is a brand listing page, not one product page."
    if kind == PRICE_TRACKER:
        return "The address is a price-tracker page, not a seller product page."
    if kind == MISSING:
        return "No address was provided."
    return "This server has no evidenced seller product page for that address."


def _digits_after(path: str, prefix: str) -> bool:
    if prefix not in path:
        return False
    product_id = path.split(prefix, 1)[1].split("/", 1)[0]
    return bool(product_id) and product_id.isdigit()


def _is_search_page(parsed) -> bool:
    path = (parsed.path or "").lower()
    host = (parsed.hostname or "").lower()
    if path.rstrip("/").endswith("/search") or "/search/" in path:
        return True
    if host == "search.naver.com" or host.endswith(".search.naver.com"):
        return True
    return False
