"""가격 히스토리 자동 수집기 — DRAFT (토요일 킥오프용)

목적: MOAT.md 9절 장벽 A — "트래픽 유무와 무관하게 매일 자동 수집".
      검색이 들어올 때만 기록하는 server.py의 log_price와 달리,
      이 스크립트는 워치리스트의 상품들을 매일 능동적으로 조회해
      prices.db의 price_history 테이블에 한 줄씩 쌓는다.

사용법:
    # 목업 모드 (API 키 없이 테스트)
    .venv/bin/python drafts/price_watcher.py --mock --db /tmp/test_prices.db

    # 실전 (NAVER_CLIENT_ID/SECRET 환경변수 필요)
    .venv/bin/python drafts/price_watcher.py

    # 매일 자동 실행 (토요일에 크론탭에 등록)
    0 6 * * * cd ~/workspace/agent-shopping-mcp && .venv/bin/python drafts/price_watcher.py >> logs/watcher.log 2>&1

설계 노트:
- server.py와 같은 DB 스키마(price_history)와 같은 product_key 규칙을 쓴다.
  → check_price_timing이 수집된 히스토리를 그대로 읽는다.
- 워치리스트는 키워드 기반 (정확한 상품 URL이 아니라 검색어로 추적).
  각 키워드당 상위 N개 결과를 모두 기록 → "이 키워드 영역의 가격대" 시계열이 됨.
- 네이버 무료 쿼터: 일 25,000건. 키워드 1,000개 × 1회 호출 = 1,000건/일 (4%).
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(BASE_DIR, "prices.db")
DEFAULT_WATCHLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "watchlist_seed.json")

NAVER_ID = os.environ.get("NAVER_CLIENT_ID", "")
NAVER_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")

# server.py와 동일한 키 규칙 (호환 필수)
def product_key_of(name: str, mall: str) -> str:
    base = re.sub(r"[^0-9a-zA-Z가-힣]", "", name)[:30]
    return f"{base}@{mall}"


def ensure_table(con):
    con.execute(
        "CREATE TABLE IF NOT EXISTS price_history("
        "product_key TEXT, name TEXT, price INTEGER, mall TEXT, ts TEXT)"
    )


def log_price(con, product_key, name, price, mall):
    con.execute(
        "INSERT INTO price_history VALUES (?,?,?,?,?)",
        (product_key, name, price, mall,
         datetime.now().isoformat(timespec="seconds")),
    )


def naver_search(keyword: str, display: int = 5):
    req = urllib.request.Request(
        "https://openapi.naver.com/v1/search/shop.json?query="
        + urllib.parse.quote(keyword) + f"&display={display}&sort=sim"
    )
    req.add_header("X-Naver-Client-Id", NAVER_ID)
    req.add_header("X-Naver-Client-Secret", NAVER_SECRET)
    with urllib.request.urlopen(req, timeout=15) as res:
        data = json.load(res)
    items = []
    for it in data.get("items", []):
        title = re.sub(r"<[^>]+>", "", it.get("title", ""))
        price = int(it.get("lprice") or 0)
        if not price:
            continue
        items.append({"name": title, "price": price,
                      "mall": it.get("mallName", "")})
    return items


# 목업 모드: API 키 없이 워치리스트 파이프라인 자체를 테스트
MOCK_POOL = [
    {"name": "오리젠 오리지날 독 2kg", "price": 42000, "mall": "쿠팡"},
    {"name": "로얄캐닌 미니 어덜트 2kg", "price": 28500, "mall": "네이버쇼핑"},
    {"name": "아카나 와일드 코스트 캣 1.8kg", "price": 39500, "mall": "쿠팡"},
    {"name": "종근당건강 락토핏 골드 2g x 50포", "price": 18900, "mall": "쿠팡"},
    {"name": "닥터린 초임계 알티지 오메가3 60캡슐", "price": 32900, "mall": "쿠팡"},
]


def mock_search(keyword: str, display: int = 5):
    kw = keyword.replace(" ", "")
    hits = [p for p in MOCK_POOL if kw[:4] in p["name"].replace(" ", "")]
    return (hits or MOCK_POOL)[:display]


def run(db_path: str, watchlist_path: str, display: int, mock: bool):
    with open(watchlist_path, encoding="utf-8") as f:
        watchlist = json.load(f)

    con = sqlite3.connect(db_path)
    ensure_table(con)

    use_mock = mock or not (NAVER_ID and NAVER_SECRET)
    total = 0
    for entry in watchlist:
        keyword = entry["keyword"]
        try:
            items = (mock_search(keyword, display) if use_mock
                     else naver_search(keyword, display))
        except Exception as e:
            print(f"[SKIP] {keyword}: {e}", file=sys.stderr)
            continue
        for p in items:
            log_price(con, product_key_of(p["name"], p["mall"]),
                      p["name"], p["price"], p["mall"])
            total += 1
        print(f"[OK] {keyword}: {len(items)}건 기록 "
              f"({'mock' if use_mock else 'naver api'})")
    con.commit()
    con.close()
    print(f"완료: 키워드 {len(watchlist)}개, 기록 {total}건 → {db_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--watchlist", default=DEFAULT_WATCHLIST)
    ap.add_argument("--display", type=int, default=5,
                    help="키워드당 기록할 상위 결과 수")
    ap.add_argument("--mock", action="store_true",
                    help="API 키 없이 목업 데이터로 파이프라인 테스트")
    args = ap.parse_args()
    run(args.db, args.watchlist, args.display, args.mock)
