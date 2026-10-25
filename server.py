"""에이전트용 최저가+타이밍 쇼핑 MCP 서버 (MVP)

에이전트가 호출하는 도구:
  - search_products  : 상품 검색 (네이버 쇼핑 API, 없으면 목업) — 과금 호출
  - check_price_timing: 지금 사기 좋은 타이밍인지 판단 (가격 히스토리 기반) — 과금 호출
  - get_buy_link     : 어필리에이트 태그가 붙은 구매 링크 반환 — 과금 호출
  - find_pet_food    : 펫 사료 조건 검색 — 과금 호출
  - compare_nutrition: 사료 영양성분 비교 — 과금 호출
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

MOCK_CATALOG = [
    {"name": "종근당건강 락토핏 골드 2g x 50포", "price": 18900, "mall": "쿠팡",
     "url": "https://www.coupang.com/", "rating": 4.8, "reviews": 152340},
    {"name": "뉴트리원 루테인 지아잔틴 30캡슐", "price": 24900, "mall": "네이버쇼핑",
     "url": "https://shopping.naver.com/", "rating": 4.7, "reviews": 89312},
    {"name": "닥터린 초임계 알티지 오메가3 60캡슐", "price": 32900, "mall": "쿠팡",
     "url": "https://www.coupang.com/", "rating": 4.8, "reviews": 67410},
    {"name": "고려은단 비타민C 1000mg 180정", "price": 15900, "mall": "11번가",
     "url": "https://www.11st.co.kr/", "rating": 4.9, "reviews": 210555},
    {"name": "삼성전자 갤럭시 버즈3 프로", "price": 279000, "mall": "네이버쇼핑",
     "url": "https://shopping.naver.com/", "rating": 4.6, "reviews": 12304},
]


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
    """상품을 검색하고 최저가 순으로 반환. 호출될 때마다 가격을 히스토리에 기록한다."""
    limit = max(1, min(limit, 20))
    if NAVER_ID and NAVER_SECRET:
        try:
            items = naver_search(keyword, limit)
            source = "naver_shopping_api"
        except Exception as e:
            return f"네이버 쇼핑 API 오류: {e}"
    else:
        items = [p for p in MOCK_CATALOG if keyword.replace(" ", "") in p["name"].replace(" ", "")][:limit]
        if not items:
            items = MOCK_CATALOG[:limit]
        source = "mock (NAVER_CLIENT_ID/SECRET 미설정)"

    for p in items:
        if p["price"]:
            log_price(product_key_of(p["name"], p["mall"]), p["name"], p["price"], p["mall"])

    items_sorted = sorted(items, key=lambda x: x["price"] or 10**18)
    lines = [f"[검색: {keyword}] (출처: {source})"]
    for i, p in enumerate(items_sorted, 1):
        lines.append(
            f"{i}. {p['name']} — {p['price']:,}원 ({p['mall']})"
            + (f" ★{p['rating']} 리뷰 {p['reviews']:,}개" if p.get("rating") else "")
        )
    if items_sorted and items_sorted[0]["price"]:
        lines.append(f"\n최저가: {items_sorted[0]['price']:,}원 ({items_sorted[0]['mall']})")
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

PET_CATALOG = [
    {"name": "오리젠 오리지날 독 2kg", "brand": "오리젠", "pet": "강아지", "age": "전연령",
     "price": 42000, "mall": "쿠팡", "url": "https://www.coupang.com/",
     "protein": 38.0, "fat": 18.0, "kcal": 3860, "main_ingredients": ["닭고기", "칠면조", "청어"],
     "grain_free": True, "allergens": [], "rating": 4.8, "reviews": 12480},
    {"name": "로얄캐닌 미니 어덜트 2kg", "brand": "로얄캐닌", "pet": "강아지", "age": "어덜트",
     "price": 28500, "mall": "네이버쇼핑", "url": "https://shopping.naver.com/",
     "protein": 27.0, "fat": 16.0, "kcal": 3650, "main_ingredients": ["쌀", "닭고기", "옥수수"],
     "grain_free": False, "allergens": ["곡물"], "rating": 4.7, "reviews": 45230},
    {"name": "아카나 와일드 코스트 캣 1.8kg", "brand": "아카나", "pet": "고양이", "age": "전연령",
     "price": 39500, "mall": "쿠팡", "url": "https://www.coupang.com/",
     "protein": 37.0, "fat": 20.0, "kcal": 3930, "main_ingredients": ["청어", "넙치", "은대구"],
     "grain_free": True, "allergens": [], "rating": 4.8, "reviews": 8930},
    {"name": "내추럴발란스 오리지널 울트라 캣 2.27kg", "brand": "내추럴발란스", "pet": "고양이",
     "age": "전연령", "price": 31000, "mall": "11번가", "url": "https://www.11st.co.kr/",
     "protein": 34.0, "fat": 18.0, "kcal": 3740, "main_ingredients": ["닭고기", "연어", "오트밀"],
     "grain_free": False, "allergens": [], "rating": 4.6, "reviews": 15620},
]


@mcp.tool()
@metered
def find_pet_food(pet: str, age: str = "", exclude_allergen: str = "",
                  grain_free_only: bool = False, max_price: int = 0,
                  sort_by: str = "protein") -> str:
    """반려동물 사료를 조건으로 검색. pet='강아지'|'고양이'.
    sort_by: 'protein'(단백질순) | 'price'(낮은가격순) | 'rating'(평점순)"""
    results = [p for p in PET_CATALOG if p["pet"] == pet]
    if age:
        results = [p for p in results if p["age"] in (age, "전연령")]
    if exclude_allergen:
        results = [p for p in results if exclude_allergen not in p["allergens"]]
    if grain_free_only:
        results = [p for p in results if p["grain_free"]]
    if max_price:
        results = [p for p in results if p["price"] <= max_price]

    key = {"protein": lambda x: -x["protein"], "price": lambda x: x["price"],
           "rating": lambda x: -x["rating"]}.get(sort_by, lambda x: -x["protein"])
    results.sort(key=key)

    for p in results:
        log_price(product_key_of(p["name"], p["mall"]), p["name"], p["price"], p["mall"])

    if not results:
        return "조건에 맞는 사료가 없어. 조건을 완화해봐."
    lines = [f"[펫사료 검색: {pet} / {len(results)}건]"]
    for i, p in enumerate(results, 1):
        gf = "그레인프리" if p["grain_free"] else "곡물포함"
        lines.append(
            f"{i}. {p['name']} — {p['price']:,}원 ({p['mall']})\n"
            f"   단백질 {p['protein']}% / 지방 {p['fat']}% / {p['kcal']}kcal/kg / {gf} / "
            f"주원료: {', '.join(p['main_ingredients'])} / ★{p['rating']} ({p['reviews']:,}개)"
        )
    return "\n".join(lines)


@mcp.tool()
@metered
def compare_nutrition(product_a: str, product_b: str) -> str:
    """두 사료의 영양성분을 나란히 비교."""
    def find(q):
        q = q.replace(" ", "")
        for p in PET_CATALOG:
            if q in p["name"].replace(" ", ""):
                return p
        return None
    a, b = find(product_a), find(product_b)
    if not a or not b:
        return "상품을 못 찾았어. find_pet_food로 먼저 검색해봐."
    rows = [
        ("가격", f"{a['price']:,}원", f"{b['price']:,}원"),
        ("단백질", f"{a['protein']}%", f"{b['protein']}%"),
        ("지방", f"{a['fat']}%", f"{b['fat']}%"),
        ("칼로리", f"{a['kcal']}kcal/kg", f"{b['kcal']}kcal/kg"),
        ("그레인프리", "O" if a["grain_free"] else "X", "O" if b["grain_free"] else "X"),
        ("평점", f"★{a['rating']} ({a['reviews']:,}개)", f"★{b['rating']} ({b['reviews']:,}개)"),
    ]
    lines = [f"[영양성분 비교]\n항목 | {a['name'][:20]} | {b['name'][:20]}"]
    for r in rows:
        lines.append(f"{r[0]} | {r[1]} | {r[2]}")
    # 간단 총평
    better_protein = a["name"] if a["protein"] > b["protein"] else b["name"]
    cheaper = a["name"] if a["price"] < b["price"] else b["name"]
    lines.append(f"\n단백질 함량은 '{better_protein[:20]}'이 높고, 가격은 '{cheaper[:20]}'이 저렴해.")
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
