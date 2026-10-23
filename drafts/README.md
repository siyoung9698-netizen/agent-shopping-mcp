# drafts/ — 토요일(10/24) 킥오프용 초안

server.py는 건드리지 않았다. 토요일에 검토 후 채택하는 방식.

## 파일

1. **price_watcher.py** — 가격 히스토리 자동 수집기
   - 워치리스트 키워드를 매일 능동 조회 → `prices.db`의 `price_history`에 기록
   - server.py와 같은 테이블·키 규칙·시간 포맷 → `check_price_timing`이 그대로 읽음
   - 목업 모드로 파이프라인 검증 완료 (12개 키워드 → 36건 기록 확인)
   - 네이버 무료 쿼터(일 25,000건) 기준: 키워드 1,000개 = 1,000건/일 ≈ 4%

2. **watchlist_seed.json** — 워치리스트 시드 12개
   - PET_CATALOG 4종 + 영양제 4종(정확한 상품명) + 카테고리 키워드 4개(영역 시세용)
   - 목표 500~1,000개는 토요일 이후 버티컬 확장하면서 추가

3. **schema_v2.sql** — 프로필·구매 기록 테이블 뼈대
   - `profiles`: 전환비용의 그릇 (펫 정보·가구·렌탈 계약 만료일)
   - `purchases`: 폐쇄루프의 그릇 (price_history와 같은 product_key로 연결)
   - 적용: `sqlite3 prices.db < drafts/schema_v2.sql`

## 토요일에 할 일 (3줄)

```bash
cp drafts/price_watcher.py .   # 또는 drafts에서 바로 실행
sqlite3 prices.db < drafts/schema_v2.sql
# 크론탭 등록 (매일 06:00):
# 0 6 * * * cd ~/workspace/agent-shopping-mcp && .venv/bin/python price_watcher.py >> logs/watcher.log 2>&1
```

## 알려진 제한

- Render 무료 티어는 크론탭이 없어서, Render 배포 시엔 로컬/별도 스케줄러에서
  price_watcher를 돌리고 DB를 동기화하거나, Starter($7/월)로 올려야 함.
  토요일에 배포 방식 확정 후 결정.
