"""에이전트용 최저가+타이밍 쇼핑 MCP 서버 (MVP)

에이전트가 호출하는 도구:
  - search_products  : 상품 검색 (네이버 키가 있으면 쇼핑 API, 없으면 catalog.json) — 과금 호출
  - check_price_timing: 기록된 가격이 최근 범위에서 낮은지 판단 — 과금 호출
  - get_buy_link     : 카탈로그에 있는 판매 상품 URL만 반환 — 과금 호출
  - find_pet_food    : 펫 사료 조건 검색 (catalog.json의 pet_food) — 과금 호출
  - compare_nutrition: 두 사료 영양성분 비교 (같은 카탈로그) — 과금 호출
  - lookup_functional_food_filing: 품목제조번호로 건강기능식품 신고 조회 — 과금 호출
  - get_usage        : 이 API 키의 이번 달 사용량 (과금하지 않음)

도구 호출에는 API 키가 필요하다. 키·한도·저장소는 metering.py 참고.
실행: .venv/bin/python server.py  (stdio 모드, MCP 클라이언트용)
"""

import functools
import inspect
import json
import logging
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from contextvars import ContextVar
from datetime import datetime, timezone

import functional_food
import product_links
from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.requests import Request
from starlette.responses import Response

import metering

# .env는 이미 설정된 환경변수(Render 등)를 덮어쓰지 않는다.
metering.load_local_env()

# Stateless: each HTTP request is its own session, so clients do not send Mcp-Session-Id.
# The MCP server id stays "agent-shopping". The public service name is the pair below.
mcp = FastMCP("agent-shopping", stateless_http=True)

# PlayMCP requires every tool description to include the service name in English
# and Korean, as in "from BuyWhen(뭐살까? 언제살까?)". These two constants are
# the only place that name is defined.
SERVICE_NAME_EN = "BuyWhen"
SERVICE_NAME_KO = "뭐살까? 언제살까?"
MAX_TOOL_RESPONSE_CHARS = 24_000
_TRUNCATION_NOTE = "\n[truncated]"

DB_PATH = os.path.join(os.path.dirname(__file__), "prices.db")
NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
# 어필리에이트: 쿠팡 파트너스 ID 등을 여기에 넣으면 구매 링크에 자동 태깅
AFFILIATE_ID = os.environ.get("AFFILIATE_ID", "")
CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")
# 검색 기본 경로는 영양제·펫푸드만. 전자제품 등 비버티컬 항목은 파일에 있어도 제외한다.
_SEARCH_CATEGORIES = {"supplement", "pet_food"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_static_catalog(path: str = CATALOG_PATH):
    """catalog.json을 한 번 읽어 상품 목록과 수동 갱신일을 반환한다."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    products = list(data.get("products") or [])
    checked = data.get("checked_at") or ""
    if not _DATE_RE.match(checked):
        dates = [p.get("checked_at") for p in products if _DATE_RE.match(p.get("checked_at") or "")]
        checked = max(dates) if dates else ""
    return products, checked


STATIC_PRODUCTS, STATIC_CHECKED_AT = load_static_catalog()


def static_source_label() -> str:
    """정적 파일 검색임을 밝히는 출처 문자열. 실시간 API로 보이지 않게 한다."""
    if _DATE_RE.match(STATIC_CHECKED_AT or ""):
        return f"static_catalog (수동 갱신 {STATIC_CHECKED_AT})"
    return "static_catalog (수동 갱신 날짜없음)"


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def _search_blob(product: dict) -> str:
    parts = [product.get("name") or "", product.get("brand") or ""]
    for kw in product.get("keywords") or []:
        parts.append(str(kw))
    return _compact(" ".join(parts))


def _query_matches(blob: str, keyword: str) -> bool:
    query = keyword.strip()
    if not query:
        return False
    if _compact(query) in blob:
        return True
    tokens = [t for t in re.split(r"\s+", query) if t]
    return len(tokens) > 1 and all(_compact(t) in blob for t in tokens)


def _static_hits(keyword: str) -> list:
    hits = []
    for product in STATIC_PRODUCTS:
        if product.get("category") not in _SEARCH_CATEGORIES:
            continue
        if not isinstance(product.get("price"), int):
            continue
        if _query_matches(_search_blob(product), keyword):
            hits.append(product)
    hits.sort(key=lambda p: p["price"])
    return hits


def pet_catalog() -> list:
    """펫 도구가 보는 유일한 목록. search_products와 같은 catalog.json."""
    return [p for p in STATIC_PRODUCTS if p.get("category") == "pet_food"]

# get_usage가 방금 조회한 스냅샷을 읽는다. 도구 인자로 키를 받지 않기 위함.
_caller_view: ContextVar[metering.UsageView | None] = ContextVar("caller_view", default=None)


def resolve_caller_key(ctx: Context | None) -> str | None:
    """HTTP 요청이면 헤더/쿼리만 보고, stdio처럼 요청이 없으면 MCP_API_KEY를 본다.

    HTTP에서 프로세스 환경변수로 폴백하지 않는다. Render에 MCP_API_KEY가
    남아 있어도 키 없는 외부 호출이 그 키로 집계되면 안 된다.
    """
    request = _http_request(ctx)
    if request is not None:
        return metering.extract_api_key(
            headers=request.headers,
            query_params=getattr(request, "query_params", None),
        )
    return os.environ.get("MCP_API_KEY", "").strip() or None


def _http_request(ctx: Context | None):
    if ctx is None:
        return None
    request_context = getattr(ctx, "_request_context", None)
    if request_context is None:
        return None
    request = getattr(request_context, "request", None)
    if request is None or not hasattr(request, "headers"):
        return None
    return request


def _tool_error(err: metering.MeterError, tool_name: str) -> CallToolResult:
    payload = dict(err.payload)
    payload["tool"] = tool_name
    text = json.dumps(payload, ensure_ascii=False)
    # 성공 응답과 같은 output schema({result: string})를 만족시키면서 isError로 실패시킨다.
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"result": text},
        isError=True,
    )


def _json_response(
    payload: dict,
    status_code: int = 200,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    headers = {"cache-control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json; charset=utf-8",
        headers=headers,
    )


def service_from_clause() -> str:
    """PlayMCP attribution, for example from BuyWhen(뭐살까? 언제살까?)."""
    return f"from {SERVICE_NAME_EN}({SERVICE_NAME_KO})"


def render_tool_description(func) -> str:
    """Fill {service} from SERVICE_NAME_EN / SERVICE_NAME_KO and keep the 1024 cap."""
    clause = service_from_clause()
    text = (inspect.getdoc(func) or "").replace("{service}", clause).strip()
    if clause not in text:
        text = f"{text}\n\nResults {clause}.".strip()
    if len(text) > 1024:
        raise ValueError(
            f"{getattr(func, '__name__', 'tool')} description is {len(text)} characters; PlayMCP allows 1024."
        )
    return text


def limit_tool_text(text: str) -> str:
    """Keep a tool result within PlayMCP's 24,000-character response limit."""
    limit = MAX_TOOL_RESPONSE_CHARS
    if len(text) <= limit:
        return text
    note = _TRUNCATION_NOTE
    if len(note) >= limit:
        return text[:limit]
    budget = limit - len(note)
    clipped = text[:budget]
    newline = clipped.rfind("\n")
    if newline >= budget // 2:
        clipped = clipped[:newline]
    return clipped + note


def limit_tool_output(value):
    if isinstance(value, str):
        return limit_tool_text(value)
    if isinstance(value, CallToolResult):
        content = []
        for block in value.content:
            if isinstance(block, TextContent):
                content.append(TextContent(type="text", text=limit_tool_text(block.text)))
            else:
                content.append(block)
        structured = value.structuredContent
        if isinstance(structured, dict) and isinstance(structured.get("result"), str):
            structured = dict(structured)
            structured["result"] = limit_tool_text(structured["result"])
        return CallToolResult(
            content=content,
            structuredContent=structured,
            isError=value.isError,
        )
    return value


def metered(fn=None, *, consume: bool = True):
    """도구 호출 앞에서 키를 확인하고, consume=True면 월간 카운터를 1 올린다."""

    def decorate(func):
        def gate(kwargs):
            ctx = kwargs.pop("ctx", None)
            key = resolve_caller_key(ctx)
            try:
                meter = metering.get_meter()
                if consume:
                    return meter.consume(key)
                return meter.inspect(key)
            except metering.MeterError as err:
                return _tool_error(err, func.__name__)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def wrapper(*args, **kwargs):
                gated = gate(kwargs)
                if isinstance(gated, CallToolResult):
                    return limit_tool_output(gated)
                token = _caller_view.set(gated)
                try:
                    return limit_tool_output(await func(*args, **kwargs))
                finally:
                    _caller_view.reset(token)

        else:

            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                gated = gate(kwargs)
                if isinstance(gated, CallToolResult):
                    return limit_tool_output(gated)
                token = _caller_view.set(gated)
                try:
                    return limit_tool_output(func(*args, **kwargs))
                finally:
                    _caller_view.reset(token)

        annotations = dict(getattr(func, "__annotations__", {}))
        annotations["ctx"] = Context
        wrapper.__annotations__ = annotations
        wrapper._requires_api_key = True
        return wrapper

    if fn is None:
        return decorate
    return decorate(fn)


# ---------- 가격 히스토리 DB ----------

def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "CREATE TABLE IF NOT EXISTS price_history("
        "product_key TEXT, name TEXT, price INTEGER, mall TEXT, ts TEXT)"
    )
    return con


def catalog_observed_ts(product: dict) -> str:
    """Timestamp of the catalog price date. Reading the file again is not a new observation."""
    checked = product.get("checked_at") or ""
    if not _DATE_RE.match(checked):
        checked = STATIC_CHECKED_AT or ""
    if _DATE_RE.match(checked):
        return f"{checked}T00:00:00"
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def log_price(product_key: str, name: str, price: int, mall: str, observed_ts: str | None = None):
    ts = observed_ts or datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    con = _db()
    existing = con.execute(
        "SELECT 1 FROM price_history WHERE product_key=? AND price=? AND ts=? LIMIT 1",
        (product_key, price, ts),
    ).fetchone()
    if existing is None:
        con.execute(
            "INSERT INTO price_history VALUES (?,?,?,?,?)",
            (product_key, name, price, mall, ts),
        )
        con.commit()
    con.close()


def price_stats(product_key: str):
    """최근 90일 최저/평균/현재가 반환"""
    con = _db()
    rows = con.execute(
        "SELECT price, ts FROM price_history WHERE product_key=? "
        "AND ts >= datetime('now','-90 days') ORDER BY ts",
        (product_key,),
    ).fetchall()
    con.close()
    if not rows:
        return None
    prices = [r[0] for r in rows]
    return {
        "count": len(rows),
        "min": min(prices),
        "avg": round(sum(prices) / len(prices)),
        "latest": prices[-1],
        "latest_ts": rows[-1][1][:10],
        "first_seen": rows[0][1][:10],
    }


def product_key_of(name: str, mall: str) -> str:
    base = re.sub(r"[^0-9a-zA-Z가-힣]", "", name)[:30]
    return f"{base}@{mall}"


# ---------- 상품 검색 ----------

def _hit_suffix(product: dict) -> str:
    bits = []
    if product.get("rating") is not None:
        bits.append(f"★{product['rating']}")
    if isinstance(product.get("reviews"), int):
        bits.append(f"리뷰 {product['reviews']:,}개")
    if _DATE_RE.match(product.get("checked_at") or ""):
        bits.append(f"기준일 {product['checked_at']}")
    return (" " + " ".join(bits)) if bits else ""


def naver_search(keyword: str, limit: int):
    req = urllib.request.Request(
        "https://openapi.naver.com/v1/search/shop.json?query="
        + urllib.parse.quote(keyword) + f"&display={limit}&sort=sim"
    )
    req.add_header("X-Naver-Client-Id", NAVER_ID)
    req.add_header("X-Naver-Client-Secret", NAVER_SECRET)
    with urllib.request.urlopen(req, timeout=15) as res:
        data = json.load(res)
    items = []
    for it in data.get("items", []):
        title = re.sub(r"<[^>]+>", "", it.get("title", ""))
        price = int(it.get("lprice") or 0)
        items.append({
            "name": title,
            "price": price,
            "mall": it.get("mallName", ""),
            "url": it.get("link", ""),
            "rating": None, "reviews": None,
        })
    return items


def _listed(annotations: ToolAnnotations):
    """docstring을 들여쓰기 없이 도구 설명으로 등록한다.

    PlayMCP는 등록 시점의 설명을 캐시하므로, 설명은 이 docstring이 기준이다.
    """

    def decorate(func):
        description = render_tool_description(func)
        func.__doc__ = description
        return mcp.tool(description=description, annotations=annotations)(func)

    return decorate


# 힌트는 사용자 환경이나 외부 시스템이 바뀌는지를 본다.
# 사용량 카운터와 가격 히스토리는 서버 내부 기록이라 읽기 전용·멱등이다.
# 외부 API를 호출할 수 있는 도구는 search_products(네이버 쇼핑)와
# lookup_functional_food_filing(식품안전나라 I0030)뿐이다.
@_listed(
    ToolAnnotations(
        title="상품 검색",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@metered
def search_products(keyword: str, limit: int = 10) -> str:
    """Search products and return matches {service}, lowest recorded price first.

    With both Naver keys set, this calls Naver Shopping and does not fall back to the catalog if that call fails. Otherwise it searches catalog.json for supplements and pet food. A static price keeps the catalog date and is not a live price. A product URL is included only for a stored seller product page. Brand listings, price trackers, and search pages are reported as no product URL. If nothing matches, the result is not filled with other products.

    keyword is the query. limit is 1 through 20, default 10. The call requires this server's API key. A missing, invalid, or expired key is rejected and is not counted. A key over its monthly quota returns an error and is not counted."""
    limit = max(1, min(limit, 20))
    queried_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if NAVER_ID and NAVER_SECRET:
        try:
            items = naver_search(keyword, limit)
            source = "naver_shopping_api"
        except Exception as e:
            return f"네이버 쇼핑 API 오류: {e}"
        for product in items:
            if product["price"]:
                log_price(
                    product_key_of(product["name"], product["mall"]),
                    product["name"],
                    product["price"],
                    product["mall"],
                )
        items_sorted = sorted(items, key=lambda x: x["price"] or 10**18)
        lines = [
            f"[검색: {keyword}] (출처: {source})",
            f"가격은 네이버 쇼핑 API 응답의 lprice이며 조회 시각은 {queried_at}이다. static_catalog 가격이 아니다.",
        ]
        url_lines = lambda product: product_links.remote_url_lines(product.get("url"))
    else:
        # 맞는 상품이 없어도 다른 카테고리로 채우지 않는다.
        items = _static_hits(keyword)[:limit]
        source = static_source_label()
        for product in items:
            if product["price"]:
                log_price(
                    product_key_of(product["name"], product["mall"]),
                    product["name"],
                    product["price"],
                    product["mall"],
                    catalog_observed_ts(product),
                )
        items_sorted = sorted(items, key=lambda x: x["price"] or 10**18)
        lines = [
            f"[검색: {keyword}] (출처: {source})",
            (
                f"가격 기준일: {STATIC_CHECKED_AT or '날짜없음'}. "
                f"static_catalog에 적힌 가격이며, 이 조회 시각({queried_at})의 시세가 아니다."
            ),
        ]
        url_lines = lambda product: product_links.catalog_url_lines(product.get("url"))

    if not items_sorted:
        lines.append("맞는 상품이 없어.")
        return "\n".join(lines)
    for index, product in enumerate(items_sorted, 1):
        mall = product.get("mall") or ""
        lines.append(f"{index}. {product['name']} — {product['price']:,}원 ({mall}){_hit_suffix(product)}")
        lines.extend(url_lines(product))
    if items_sorted[0]["price"]:
        checked = items_sorted[0].get("checked_at") or STATIC_CHECKED_AT or ""
        if source == "naver_shopping_api":
            lines.append(
                f"\n응답 가격 중 가장 낮음: {items_sorted[0]['price']:,}원 "
                f"({items_sorted[0].get('mall') or ''}), 조회 시각 {queried_at}"
            )
        else:
            lines.append(
                f"\n기록 가격 중 가장 낮음: {items_sorted[0]['price']:,}원 "
                f"({items_sorted[0].get('mall') or ''}), 기준일 {checked or '날짜없음'}"
            )
    return "\n".join(lines)


@_listed(
    ToolAnnotations(
        title="가격 타이밍",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@metered
def check_price_timing(product_name: str, mall: str = "") -> str:
    """Compare the latest stored price with this server's recent history {service} and say whether it is low, typical, or high.

    Prices come only from earlier searches recorded on this server. No external price API is called. A static catalog row keeps its catalog date and is not a price observed at this call. With fewer than two records, the tool reports the count and does not guess. Otherwise it returns the latest recorded price and its record date, the 90-day low, the average, and one line: near the low when that price is at or below 102 percent of the low, at or below the average, or the percent above the average.

    product_name is the product name. mall is the seller. An empty mall matches only records stored with an empty seller. The lookup keeps letters and digits from the name and uses the first 30. The call requires this server's API key. A missing, invalid, or expired key is rejected and is not counted. A key over its monthly quota returns an error and is not counted."""
    key = product_key_of(product_name, mall)
    st = price_stats(key)
    if not st or st["count"] < 2:
        return (
            f"'{product_name}' 가격 기록이 {st['count'] if st else 0}건뿐이야. "
            "같은 정적 카탈로그 가격을 다시 읽어도 새 관측으로 쌓이지 않아. "
            "기록이 2건 미만이면 타이밍을 판단하지 않아."
        )
    cur, mn, avg = st["latest"], st["min"], st["avg"]
    if cur <= mn * 1.02:
        verdict = "기록된 가격이 최근 90일 최저 기록과 같거나 2% 이내야."
    elif cur <= avg:
        verdict = f"평균({avg:,}원)보다 낮은 기록이야."
    else:
        pct = round((cur - avg) / avg * 100)
        verdict = f"평균보다 {pct}% 높은 기록이야."
    return (
        f"[가격 타이밍: {product_name}]\n"
        f"최근 기록 {cur:,}원 (기록일 {st['latest_ts']}) / 90일 최저 {mn:,}원 / 평균 {avg:,}원 "
        f"(기록 {st['count']}건, {st['first_seen']}~)\n{verdict}"
    )


@_listed(
    ToolAnnotations(
        title="구매 링크",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@metered
def get_buy_link(product_url: str, product_name: str = "") -> str:
    """Return an evidenced seller product URL {service}. This does not open the page or take payment.

    The URL is returned only when catalog.json stores that same address as a seller product page. Brand listings, price trackers, search pages, and any other address are unavailable and are not turned into a product link. A partner_id parameter is added only to an evidenced coupang.com product page when an affiliate id is set.

    product_url is the address. product_name is optional and is not written into the URL. The call requires this server's API key. A missing, invalid, or expired key is rejected and is not counted. A key over its monthly quota returns an error and is not counted."""
    product = product_links.find_product_by_url(STATIC_PRODUCTS, product_url)
    if product is None:
        reason = product_links.unavailable_reason(product_url)
        return f"Product link unavailable\n{reason}\nNo affiliate tag was added."
    url = str(product.get("url") or "").strip()
    note = "No affiliate tag is configured. The original URL is returned unchanged."
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if AFFILIATE_ID and host.endswith("coupang.com"):
        parsed = urllib.parse.urlparse(url)
        pairs = [
            (key, value)
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if key != "partner_id"
        ]
        pairs.append(("partner_id", AFFILIATE_ID))
        url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(pairs)))
        note = "An affiliate tag was added to the URL."
    elif AFFILIATE_ID:
        note = "The original URL is returned unchanged."
    checked = product.get("checked_at") or STATIC_CHECKED_AT or "날짜없음"
    return (
        "Product link\n"
        f"{url}\n"
        "source: static_catalog\n"
        f"catalog_checked_at: {checked}\n"
        "The catalog date is not the time of this call. The page was not opened.\n"
        f"{note}"
    )


# ---------- 펫 버티컬 확장 ----------

def _age_ok(product_age: str, query: str) -> bool:
    """카탈로그 연령(퍼피/키튼/성견/성묘/시니어)과 질의 연령을 맞춘다.

    '어덜트'는 성견·성묘를 함께 가리킨다. '전연령' 상품은 어떤 연령 필터에도 남긴다.
    """
    if not query:
        return True
    age = (product_age or "").strip()
    q = query.strip()
    if age == "전연령" or q == "전연령":
        return True
    if q == "어덜트":
        return age in {"성견", "성묘", "어덜트"}
    if q in {"성견", "성묘"}:
        return age == q or age == "어덜트"
    return age == q


def _num_text(value, suffix: str) -> str:
    if not isinstance(value, (int, float)):
        return "미확인"
    return f"{value}{suffix}"


def _score_text(product: dict) -> str:
    rating = product.get("rating")
    reviews = product.get("reviews")
    if rating is not None and isinstance(reviews, int):
        return f"★{rating} ({reviews:,}개)"
    if rating is not None:
        return f"★{rating}"
    if isinstance(reviews, int):
        return f"리뷰 {reviews:,}개"
    return "평점 미확인"


def _pet_sort_key(sort_by: str):
    if sort_by == "price":
        return lambda p: (p.get("price") is None, p.get("price") or 0)
    if sort_by == "rating":
        return lambda p: (p.get("rating") is None, -(p.get("rating") or 0), p.get("price") or 0)
    return lambda p: (p.get("protein") is None, -(p.get("protein") or 0), p.get("price") or 0)


def _find_pet_product(query: str):
    compact = _compact(query)
    if not compact:
        return None
    hits = []
    for product in pet_catalog():
        name = _compact(product.get("name") or "")
        pid = _compact(product.get("id") or "")
        if compact in name or compact == pid:
            hits.append(product)
    if not hits:
        return None
    hits.sort(key=lambda p: len(p.get("name") or ""))
    return hits[0]


@_listed(
    ToolAnnotations(
        title="펫사료 검색",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@metered
def find_pet_food(pet: str, age: str = "", exclude_allergen: str = "",
                  grain_free_only: bool = False, max_price: int = 0,
                  sort_by: str = "protein") -> str:
    """Filter pet food in catalog.json and return the matches {service}. No external API is called.

    Only catalog prices and nutrition values are used. Missing numbers are shown as unknown. If nothing matches, the result says so.

    pet is 강아지 (dog) or 고양이 (cat). age is 퍼피, 키튼, 성견, 성묘, 시니어, 어덜트, or 전연령; empty skips the age filter. 어덜트 matches 성견 and 성묘. age 전연령 keeps every age, and a product aged 전연령 passes any age filter. exclude_allergen drops a product when that string is in its allergen list; empty drops nothing. grain_free_only, when true, keeps grain-free products. max_price of 0 means no cap; any other value keeps products at or below that price. sort_by is protein (highest protein, default), price (lowest price), or rating (highest rating). Any other sort_by sorts like protein. The call requires this server's API key. A missing, invalid, or expired key is rejected and is not counted. A key over its monthly quota returns an error and is not counted."""
    pet = pet.strip()
    results = [p for p in pet_catalog() if p.get("pet") == pet]
    results = [p for p in results if _age_ok(p.get("age") or "", age)]
    if exclude_allergen:
        results = [p for p in results if exclude_allergen not in (p.get("allergens") or [])]
    if grain_free_only:
        results = [p for p in results if p.get("grain_free")]
    if max_price:
        results = [p for p in results if isinstance(p.get("price"), int) and p["price"] <= max_price]

    results.sort(key=_pet_sort_key(sort_by))

    for p in results:
        if isinstance(p.get("price"), int) and p["price"]:
            log_price(
                product_key_of(p["name"], p.get("mall") or ""),
                p["name"],
                p["price"],
                p.get("mall") or "",
                catalog_observed_ts(p),
            )

    source = static_source_label()
    if not results:
        return (
            f"조건에 맞는 사료가 없어. 조건을 완화해봐. (출처: {source})\n"
            "가격은 카탈로그 기준일이며 이 조회 시각의 시세가 아니다."
        )
    lines = [
        f"[펫사료 검색: {pet} / {len(results)}건] (출처: {source})",
        "가격은 카탈로그 기준일이며 이 조회 시각의 시세가 아니다.",
    ]
    for i, p in enumerate(results, 1):
        gf = "그레인프리" if p.get("grain_free") else "곡물포함"
        ingredients = p.get("main_ingredients") or []
        ing = ", ".join(ingredients) if ingredients else "미확인"
        price = f"{p['price']:,}원" if isinstance(p.get("price"), int) else "미확인"
        lines.append(
            f"{i}. {p['name']} — {price} ({p.get('mall') or ''})\n"
            f"   단백질 {_num_text(p.get('protein'), '%')} / 지방 {_num_text(p.get('fat'), '%')} / "
            f"칼로리 {_num_text(p.get('kcal'), 'kcal/kg')} / {gf} / 주원료: {ing} / {_score_text(p)}"
        )
        lines.extend(product_links.catalog_url_lines(p.get("url")))
    return "\n".join(lines)


@_listed(
    ToolAnnotations(
        title="영양성분 비교",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@metered
def compare_nutrition(product_a: str, product_b: str) -> str:
    """Compare two pet foods in catalog.json side by side {service}: price, protein, fat, calories, grain-free, and rating.

    Values missing from the catalog are shown as unknown and are not invented. No external API is called.

    product_a and product_b are part of a product name or a product id. Spaces are ignored and letter case does not matter. A name matches when it contains the query, and an id matches when it is equal. If several match, the shorter name is used. If either product is missing, the result says it was not found. The call requires this server's API key. A missing, invalid, or expired key is rejected and is not counted. A key over its monthly quota returns an error and is not counted."""
    a, b = _find_pet_product(product_a), _find_pet_product(product_b)
    source = static_source_label()
    if not a or not b:
        return f"상품을 못 찾았어. find_pet_food로 먼저 검색해봐. (출처: {source})"

    def price_text(p):
        return f"{p['price']:,}원" if isinstance(p.get("price"), int) else "미확인"

    rows = [
        ("가격", price_text(a), price_text(b)),
        ("단백질", _num_text(a.get("protein"), "%"), _num_text(b.get("protein"), "%")),
        ("지방", _num_text(a.get("fat"), "%"), _num_text(b.get("fat"), "%")),
        ("칼로리", _num_text(a.get("kcal"), "kcal/kg"), _num_text(b.get("kcal"), "kcal/kg")),
        ("그레인프리", "O" if a.get("grain_free") else "X", "O" if b.get("grain_free") else "X"),
        ("평점", _score_text(a), _score_text(b)),
    ]
    lines = [
        f"[영양성분 비교] (출처: {source})",
        f"항목 | {a['name'][:20]} | {b['name'][:20]}",
    ]
    for row in rows:
        lines.append(f"{row[0]} | {row[1]} | {row[2]}")
    pa, pb = a.get("protein"), b.get("protein")
    if isinstance(pa, (int, float)) and isinstance(pb, (int, float)) and pa != pb:
        richer = a if pa > pb else b
        protein_note = f"단백질 함량은 '{richer['name'][:20]}'이 높아."
    elif isinstance(pa, (int, float)) and isinstance(pb, (int, float)):
        protein_note = "단백질 함량은 같아."
    else:
        protein_note = "단백질 수치는 카탈로그에 없어."
    if isinstance(a.get("price"), int) and isinstance(b.get("price"), int) and a["price"] != b["price"]:
        cheaper = a if a["price"] < b["price"] else b
        price_note = f"가격은 '{cheaper['name'][:20]}'이 저렴해."
    else:
        price_note = ""
    lines.append("\n" + " ".join(part for part in (protein_note, price_note) if part))
    return "\n".join(lines)


@_listed(
    ToolAnnotations(
        title="사용량 조회",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
@metered(consume=False)
def get_usage() -> str:
    """Return this request's API-key usage for the current UTC month as JSON {service}.

    The object includes calls used, calls remaining, the monthly quota, the month, the next reset time in UTC, and the metered tool names. It includes a short key_id, not the raw key, and not any other key's usage.

    This call takes no arguments and does not count against the quota. A missing, invalid, or expired key is rejected. A key that is already over its quota can still be read."""
    view = _caller_view.get()
    if view is None:
        raise RuntimeError("사용량 컨텍스트가 없습니다.")
    return json.dumps(view.to_dict(), ensure_ascii=False)


def _filing_error(err: functional_food.FilingError, report_no: str | None = None) -> CallToolResult:
    payload = functional_food.error_payload(err, report_no)
    text = json.dumps(payload, ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"result": text},
        isError=True,
    )


@_listed(
    ToolAnnotations(
        title="품목신고 조회",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
)
@metered
def lookup_functional_food_filing(report_no: str) -> str:
    """Look up one health functional food filing by the exact 품목제조번호 {service}.

    Calls MFDS FoodSafetyKorea service I0030. The number is matched as text, so leading zeroes stay. Names and brands are not searched. Present filing fields are returned, empty fields are listed, and the documentation URL and query time are included. Price and a purchase URL are not in this source. The filing text is not an efficacy finding, personal advice, or a government endorsement.

    report_no is the 품목제조번호. Set FOODSAFETYKOREA_API_KEY on the server. If it is empty, the tool returns filing_api_unconfigured and does not invent a filing. The call requires this server's API key. A missing, invalid, or expired key is rejected and is not counted. A key over its monthly quota returns an error and is not counted."""
    key = os.environ.get(functional_food.ENV_NAME, "")
    shown = report_no if isinstance(report_no, str) and len(report_no) <= 64 else None
    try:
        report = functional_food.validate_report_no(report_no)
    except functional_food.FilingError as err:
        return _filing_error(err, shown)
    if not key.strip():
        return _filing_error(functional_food.unconfigured_error(), report)
    try:
        payload = functional_food.fetch_i0030(report, key.strip())
    except functional_food.FilingError as err:
        return _filing_error(err, report)
    except Exception:
        return _filing_error(
            functional_food.FilingError("upstream_error", "공식 API 호출에 실패했다."),
            report,
        )
    return json.dumps(payload, ensure_ascii=False)


@mcp.custom_route("/health", methods=["GET"])
async def health_route(_request: Request) -> Response:
    """프로세스 생존 확인. 테넌트 사용량은 포함하지 않는다."""
    return _json_response({"status": "ok", "service": "agent-shopping"})


@mcp.custom_route("/usage", methods=["GET"])
async def usage_route(request: Request) -> Response:
    """이 요청의 API 키에 대한 사용량만 반환한다."""
    key = metering.extract_api_key(headers=request.headers, query_params=request.query_params)
    try:
        view = metering.get_meter().inspect(key)
    except metering.MeterError as err:
        status = 503 if err.code == "server_unconfigured" else 401
        extra = {"WWW-Authenticate": _auth_challenge(err.code)} if status == 401 else None
        return _json_response(err.payload, status_code=status, extra_headers=extra)
    return _json_response(view.to_dict())


AUTH_DIAGNOSTIC_LOGGER_NAME = "agent_shopping.auth"
AUTH_REJECTION_EVENT = "mcp_tool_auth_rejected"
UNKNOWN_AUTH_TOOL = "unknown"
AUTH_DIAGNOSTIC_TOOLS = (
    "search_products",
    "check_price_timing",
    "get_buy_link",
    "find_pet_food",
    "compare_nutrition",
    "lookup_functional_food_filing",
    "get_usage",
)
AUTH_REJECTION_LOG_FIELDS = (
    "event",
    "timestamp",
    "tool",
    "error",
    "auth_source",
    "authorization_scheme",
    "x_api_key_present",
)
_AUTH_LOG_LINE_RE = re.compile(r"[\x20-\x7e]+")
_AUTH_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_auth_diagnostic_logger = logging.getLogger(AUTH_DIAGNOSTIC_LOGGER_NAME)
_auth_diagnostic_logger.setLevel(logging.WARNING)


def _allowlisted(value: object, allowed: tuple[str, ...], fallback: str) -> str:
    if isinstance(value, str):
        for item in allowed:
            if value == item:
                return item
    return fallback


def _allowlisted_auth_error(code: object) -> str | None:
    if isinstance(code, str):
        for item in metering.AUTH_FAILURE_CODES:
            if code == item:
                return item
    return None


def _utc_auth_timestamp(clock) -> str:
    now = datetime.now(timezone.utc) if clock is None else clock()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _auth_rejection_payload(tool_name, error_code, selection, clock) -> dict | None:
    error = _allowlisted_auth_error(error_code)
    if error is None:
        return None
    payload = {
        "event": AUTH_REJECTION_EVENT,
        "timestamp": _utc_auth_timestamp(clock),
        "tool": _allowlisted(tool_name, AUTH_DIAGNOSTIC_TOOLS, UNKNOWN_AUTH_TOOL),
        "error": error,
        "auth_source": _allowlisted(
            getattr(selection, "auth_source", None),
            metering.AUTH_SOURCES,
            metering.AUTH_SOURCE_NONE,
        ),
        "authorization_scheme": _allowlisted(
            getattr(selection, "authorization_scheme", None),
            metering.AUTHORIZATION_SCHEMES,
            metering.AUTHORIZATION_SCHEME_NON_BEARER,
        ),
        "x_api_key_present": getattr(selection, "x_api_key_present", False) is True,
    }
    if list(payload) != list(AUTH_REJECTION_LOG_FIELDS):
        return None
    if payload["event"] != AUTH_REJECTION_EVENT:
        return None
    if payload["tool"] not in (*AUTH_DIAGNOSTIC_TOOLS, UNKNOWN_AUTH_TOOL):
        return None
    if payload["error"] not in metering.AUTH_FAILURE_CODES:
        return None
    if payload["auth_source"] not in metering.AUTH_SOURCES:
        return None
    if payload["authorization_scheme"] not in metering.AUTHORIZATION_SCHEMES:
        return None
    if payload["x_api_key_present"] is not True and payload["x_api_key_present"] is not False:
        return None
    if _AUTH_TIMESTAMP_RE.fullmatch(payload["timestamp"]) is None:
        return None
    return payload


def emit_auth_rejection(tool_name, error_code, selection, *, clock=None) -> None:
    """Emit one secret-free JSON line for a rejected HTTP tools/call.

    The message fields are event, timestamp (UTC from this server's clock),
    tool (an allowlisted name, or unknown), error (missing_api_key,
    invalid_api_key, or expired_api_key), auth_source (bearer, x-api-key,
    query, or none), authorization_scheme (bearer, non_bearer, or absent),
    and x_api_key_present. The key, headers, query string, and request body
    are not accepted and are not written.
    """
    try:
        payload = _auth_rejection_payload(tool_name, error_code, selection, clock)
        if payload is None:
            return
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        if _AUTH_LOG_LINE_RE.fullmatch(line) is None:
            return
        _auth_diagnostic_logger.warning(line)
    except Exception:
        return


def _auth_challenge(code: str) -> str:
    """RFC 6750 challenge. The JSON body keeps the existing error code."""
    if code == "missing_api_key":
        error, description = "invalid_request", "API key required"
    elif code == "expired_api_key":
        error, description = "invalid_token", "API key expired"
    else:
        error, description = "invalid_token", "API key is invalid"
    return (
        f'Bearer realm="agent-shopping", error="{error}", '
        f'error_description="{description}"'
    )


def _scope_headers(scope) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in scope.get("headers") or ():
        headers[key.decode("latin-1")] = value.decode("latin-1")
    return headers


def _scope_query(scope) -> dict[str, str]:
    parsed = urllib.parse.parse_qs((scope.get("query_string") or b"").decode("latin-1"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items() if values}


def _tools_call_names(body: bytes) -> list[str]:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []
    messages = payload if isinstance(payload, list) else [payload]
    names: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            continue
        params = message.get("params")
        if not isinstance(params, dict):
            continue
        name = params.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return names


def _key_required_tool(names: list[str]) -> str | None:
    for name in names:
        tool = mcp._tool_manager.get_tool(name)
        if tool is not None and getattr(tool.fn, "_requires_api_key", False):
            return name
    return None


def _unauthorized(err: metering.MeterError, tool_name: str) -> Response:
    payload = dict(err.payload)
    payload["tool"] = tool_name
    return _json_response(
        payload,
        status_code=401,
        extra_headers={"WWW-Authenticate": _auth_challenge(err.code)},
    )


async def _buffer_http_body(receive):
    messages = []
    chunks = []
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks), messages


def _replay(messages, receive):
    pending = list(messages)

    async def replay():
        if pending:
            return pending.pop(0)
        return await receive()

    return replay


async def _http_disconnect():
    return {"type": "http.disconnect"}


class ToolCallAuthMiddleware:
    """Reject key-gated tools/call with HTTP 401 before the MCP handler runs.

    initialize, tools/list, and every other method stay open. The key is read
    the same way as the tools: Authorization Bearer, then X-API-Key, then
    ?api_key=. Quota is not an auth failure and is still decided inside the tool.
    A rejection also emits one fixed diagnostic line. That line is not part of
    the auth decision or the response.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        path = (scope.get("path") or "").rstrip("/") or "/"
        if path != "/mcp":
            await self.app(scope, receive, send)
            return

        body, messages = await _buffer_http_body(receive)
        tool_name = _key_required_tool(_tools_call_names(body))
        if tool_name is not None:
            headers = _scope_headers(scope)
            query_params = _scope_query(scope)
            failure = metering.get_meter().auth_failure(
                metering.extract_api_key(headers=headers, query_params=query_params)
            )
            if failure is not None:
                try:
                    emit_auth_rejection(
                        tool_name,
                        failure.code,
                        metering.describe_auth_selection(
                            headers=headers,
                            query_params=query_params,
                        ),
                    )
                except Exception:
                    pass
                await _unauthorized(failure, tool_name)(scope, _http_disconnect, send)
                return

        await self.app(scope, _replay(messages, receive), send)


_build_streamable_http_app = mcp.streamable_http_app


def streamable_http_app():
    app = _build_streamable_http_app()
    app.add_middleware(ToolCallAuthMiddleware)
    return app


mcp.streamable_http_app = streamable_http_app


if __name__ == "__main__":
    try:
        metering.get_meter()
    except ValueError as exc:
        raise SystemExit(f"미터링 설정 오류: {exc}") from exc
    # MCP_TRANSPORT=stdio (기본, 로컬 MCP 클라이언트용)
    # MCP_TRANSPORT=streamable-http (원격 배포용: Render/Railway 등에서 HTTP로 노출)
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        # Render 등은 PORT 환경변수로 포트를 지정하고 외부 접속용 0.0.0.0 바인딩 필요.
        # (FastMCP 생성자 기본값이 환경변수보다 우선 적용되므로 여기서 직접 덮어씀)
        mcp.settings.host = os.environ.get("HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        # 주의: FastMCP는 생성 시점 host가 localhost면 DNS 리바인딩 보호를 켜고
        # 허용 호스트를 localhost로 고정한다. host만 0.0.0.0으로 바꿔서는 Render
        # 공개 호스트 요청이 421 Misdirected Request로 막히므로 허용 호스트도
        # 함께 갱신해야 한다. (Render 서비스명을 바꾸면 아래 호스트도 바꿀 것)
        from mcp.server.transport_security import TransportSecuritySettings

        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "agent-shopping-mcp.onrender.com",
                "agent-shopping-mcp.onrender.com:*",
                "localhost:*",
                "127.0.0.1:*",
            ],
        )
    mcp.run(transport=transport)
