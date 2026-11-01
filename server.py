"""에이전트용 최저가+타이밍 쇼핑 MCP 서버 (MVP)

에이전트가 호출하는 도구:
  - search_products  : 상품 검색 (네이버 키가 있으면 쇼핑 API, 없으면 catalog.json) — 과금 호출
  - check_price_timing: 지금 사기 좋은 타이밍인지 판단 (가격 히스토리 기반) — 과금 호출
  - get_buy_link     : 어필리에이트 태그가 붙은 구매 링크 반환 — 과금 호출
  - find_pet_food    : 펫 사료 조건 검색 (catalog.json의 pet_food) — 과금 호출
  - compare_nutrition: 두 사료 영양성분 비교 (같은 카탈로그) — 과금 호출
  - get_usage        : 이 API 키의 이번 달 사용량 (과금하지 않음)

도구 호출에는 API 키가 필요하다. 키·한도·저장소는 metering.py 참고.
실행: .venv/bin/python server.py  (stdio 모드, MCP 클라이언트용)
"""

import functools
import inspect
import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from contextvars import ContextVar
from datetime import datetime

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from starlette.requests import Request
from starlette.responses import Response

import metering

# .env는 이미 설정된 환경변수(Render 등)를 덮어쓰지 않는다.
metering.load_local_env()

mcp = FastMCP("agent-shopping")

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


def _json_response(payload: dict, status_code: int = 200) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json; charset=utf-8",
        headers={"cache-control": "no-store"},
    )


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
                    return gated
                token = _caller_view.set(gated)
                try:
                    return await func(*args, **kwargs)
                finally:
                    _caller_view.reset(token)

        else:

            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                gated = gate(kwargs)
                if isinstance(gated, CallToolResult):
                    return gated
                token = _caller_view.set(gated)
                try:
                    return func(*args, **kwargs)
                finally:
                    _caller_view.reset(token)

        annotations = dict(getattr(func, "__annotations__", {}))
        annotations["ctx"] = Context
        wrapper.__annotations__ = annotations
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


def log_price(product_key: str, name: str, price: int, mall: str):
    con = _db()
    con.execute(
        "INSERT INTO price_history VALUES (?,?,?,?,?)",
        (product_key, name, price, mall, datetime.now().isoformat(timespec="seconds")),
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
        bits.append(f"확인 {product['checked_at']}")
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


@mcp.tool()
@metered
def search_products(keyword: str, limit: int = 10) -> str:
    """상품을 검색하고 최저가 순으로 반환. 호출될 때마다 가격을 히스토리에 기록한다.

    네이버 키가 없으면 catalog.json(영양제·펫푸드)을 상품명·키워드로 검색한다.
    출처는 static_catalog이며 실시간 검색이 아니다."""
    limit = max(1, min(limit, 20))
    if NAVER_ID and NAVER_SECRET:
        try:
            items = naver_search(keyword, limit)
            source = "naver_shopping_api"
        except Exception as e:
            return f"네이버 쇼핑 API 오류: {e}"
    else:
        # 맞는 상품이 없어도 다른 카테고리로 채우지 않는다.
        items = _static_hits(keyword)[:limit]
        source = static_source_label()

    for p in items:
        if p["price"]:
            log_price(product_key_of(p["name"], p["mall"]), p["name"], p["price"], p["mall"])

    items_sorted = sorted(items, key=lambda x: x["price"] or 10**18)
    lines = [f"[검색: {keyword}] (출처: {source})"]
    if not items_sorted:
        lines.append("맞는 상품이 없어.")
        return "\n".join(lines)
    for i, p in enumerate(items_sorted, 1):
        mall = p.get("mall") or ""
        lines.append(f"{i}. {p['name']} — {p['price']:,}원 ({mall}){_hit_suffix(p)}")
    if items_sorted[0]["price"]:
        lines.append(f"\n최저가: {items_sorted[0]['price']:,}원 ({items_sorted[0].get('mall') or ''})")
    return "\n".join(lines)


@mcp.tool()
@metered
def check_price_timing(product_name: str, mall: str = "") -> str:
    """지금 사기 좋은 타이밍인지 판단. 최근 90일 히스토리와 비교한다."""
    key = product_key_of(product_name, mall)
    st = price_stats(key)
    if not st or st["count"] < 2:
        return (
            f"'{product_name}' 가격 데이터가 아직 {st['count'] if st else 0}건뿐이야. "
            "며칠간 search_products로 가격을 모으면 타이밍 판단이 가능해져."
        )
    cur, mn, avg = st["latest"], st["min"], st["avg"]
    if cur <= mn * 1.02:
        verdict = "지금이 최근 90일 최저가 수준이야. 사기 좋은 타이밍."
    elif cur <= avg:
        verdict = f"평균({avg:,}원)보다 싸. 무난하게 살 만해."
    else:
        pct = round((cur - avg) / avg * 100)
        verdict = f"평균보다 {pct}% 비싸. 급하지 않으면 며칠 기다려봐."
    return (
        f"[가격 타이밍: {product_name}]\n"
        f"현재가 {cur:,}원 / 90일 최저 {mn:,}원 / 평균 {avg:,}원 "
        f"(기록 {st['count']}건, {st['first_seen']}~)\n{verdict}"
    )


@mcp.tool()
@metered
def get_buy_link(product_url: str, product_name: str = "") -> str:
    """구매 링크 반환. AFFILIATE_ID가 설정되어 있으면 어필리에이트 태그를 붙인다."""
    url = product_url
    note = "어필리에이트 미설정 — 원본 링크 반환"
    if AFFILIATE_ID and "coupang.com" in url:
        # 쿠팡 파트너스: 실제 운영 시 파트너스 API로 단축링크 생성 권장
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}partner_id={AFFILIATE_ID}"
        note = f"어필리에이트 태그 적용됨 ({AFFILIATE_ID})"
    return f"[구매 링크]\n{url}\n({note})"


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


@mcp.tool()
@metered
def find_pet_food(pet: str, age: str = "", exclude_allergen: str = "",
                  grain_free_only: bool = False, max_price: int = 0,
                  sort_by: str = "protein") -> str:
    """반려동물 사료를 조건으로 검색. pet='강아지'|'고양이'.
    age: '퍼피'|'키튼'|'성견'|'성묘'|'시니어'|'어덜트'(성견·성묘).
    sort_by: 'protein'(단백질순) | 'price'(낮은가격순) | 'rating'(평점순).
    영양 수치가 카탈로그에 없으면 미확인으로 표시하고 가격순으로 넘긴다."""
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
            log_price(product_key_of(p["name"], p.get("mall") or ""), p["name"], p["price"], p.get("mall") or "")

    source = static_source_label()
    if not results:
        return f"조건에 맞는 사료가 없어. 조건을 완화해봐. (출처: {source})"
    lines = [f"[펫사료 검색: {pet} / {len(results)}건] (출처: {source})"]
    for i, p in enumerate(results, 1):
        gf = "그레인프리" if p.get("grain_free") else "곡물포함"
        ingredients = p.get("main_ingredients") or []
        ing = ", ".join(ingredients) if ingredients else "미확인"
        lines.append(
            f"{i}. {p['name']} — {p['price']:,}원 ({p.get('mall') or ''})\n"
            f"   단백질 {_num_text(p.get('protein'), '%')} / 지방 {_num_text(p.get('fat'), '%')} / "
            f"칼로리 {_num_text(p.get('kcal'), 'kcal/kg')} / {gf} / 주원료: {ing} / {_score_text(p)}"
        )
    return "\n".join(lines)


@mcp.tool()
@metered
def compare_nutrition(product_a: str, product_b: str) -> str:
    """두 사료의 영양성분을 나란히 비교. 수치는 catalog.json에 있는 값만 쓴다."""
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


@mcp.tool(annotations=ToolAnnotations(title="사용량 조회", readOnlyHint=True))
@metered(consume=False)
def get_usage() -> str:
    """이 API 키의 이번 달 호출 수, 남은 한도, 초기화 시각을 반환한다.

    검색·비교·구매 링크 도구와 달리 이 호출은 사용량에 포함되지 않는다.
    다른 키의 사용량은 반환하지 않는다.
    """
    view = _caller_view.get()
    if view is None:
        raise RuntimeError("사용량 컨텍스트가 없습니다.")
    return json.dumps(view.to_dict(), ensure_ascii=False)


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
        return _json_response(err.payload, status_code=status)
    return _json_response(view.to_dict())


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
