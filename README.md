# 에이전트용 최저가+타이밍 쇼핑 MCP (MVP)

AI 에이전트가 호출하는 쇼핑 도구 서버. 에이전트가 이 서버를 경유해 상품을 팔면
어필리에이트 수수료가 발생한다.

## 도구

| 도구 | 하는 일 | 호출 1회로 집계 |
|---|---|---|
| `search_products` | 상품 검색 → 최저가순 정렬. 호출될 때마다 가격을 DB에 기록. 네이버 키가 없으면 `catalog.json` | 예 |
| `check_price_timing` | 90일 가격 히스토리와 비교해 "지금 살 때인지" 판단 | 예 |
| `get_buy_link` | 어필리에이트 태그가 붙은 구매 링크 반환 | 예 |
| `find_pet_food` | `catalog.json`의 펫푸드를 조건으로 검색 | 예 |
| `compare_nutrition` | 같은 카탈로그의 두 사료 영양성분을 비교 | 예 |
| `lookup_functional_food_filing` | 품목제조번호로 건강기능식품 신고를 조회 | 예 |
| `get_usage` | 이 API 키의 이번 달 사용량·남은 한도·초기화 시각 | 아니오 |

## API 키와 호출 미터링

도구를 호출하려면 API 키가 필요하다. HTTP `tools/call`에서 키가 없거나(`missing_api_key`),
등록되지 않았거나(`invalid_api_key`), 만료되었으면(`expired_api_key`) 응답은 HTTP 401이고
`WWW-Authenticate: Bearer`를 포함한다. 본문은 같은 오류 JSON이다.
한도를 넘긴 호출은 키가 유효하므로 기존처럼 HTTP 200의 도구 오류 `quota_exceeded`이다.
stdio처럼 HTTP가 아닌 호출은 상태 코드가 없어 같은 오류 JSON을 도구 결과로 돌려준다.
`initialize`, `tools/list`, `GET /health`는 키 없이 된다.
Streamable HTTP는 세션을 저장하지 않는다. `Mcp-Session-Id` 없이 요청마다 호출할 수 있다.
결제는 아직 없다. 운영자가 환경변수로 키를 넣고, 키마다 월간 무료 한도를 둔다.

### 빌더가 키를 받는 방법

셀프 가입은 없다. 서버 운영자가 `MCP_API_KEYS`에 키를 추가하고 그 문자열을 전달한다.
로컬과 PlayMCP 시험용으로 `.env.example`에 `dev-demo-key`가 있다.
이 값은 비밀이 아니므로, 공개 URL에 붙일 때는 운영자만 아는 키로 바꾼다.

### 클라이언트에 키를 넣는 방법

우선순위는 `Authorization: Bearer <키>`, 그다음 `X-API-Key: <키>`,
헤더를 못 넣는 클라이언트만 URL 쿼리 `?api_key=<키>` 이다.
헤더가 있으면 쿼리의 다른 키는 무시한다. 쿼리는 접근 로그에 남을 수 있다.

원격 (Cursor, Claude, 그 외 Streamable HTTP 클라이언트):

```json
{
  "mcpServers": {
    "agent-shopping": {
      "url": "https://agent-shopping-mcp.onrender.com/mcp",
      "headers": { "Authorization": "Bearer dev-demo-key" }
    }
  }
}
```

헤더를 지원하지 않는 클라이언트 (PlayMCP에서 URL만 넣는 경우):

```text
https://agent-shopping-mcp.onrender.com/mcp?api_key=dev-demo-key
```

로컬 stdio. `MCP_API_KEYS`는 서버가 허용하는 키, `MCP_API_KEY`는 이 프로세스가 제시하는 키다.
HTTP로 들어온 요청은 `MCP_API_KEY`를 보지 않는다.

```json
{
  "mcpServers": {
    "agent-shopping": {
      "command": "python",
      "args": ["server.py"],
      "env": {
        "MCP_API_KEYS": "dev-demo-key",
        "MCP_API_KEY": "dev-demo-key"
      }
    }
  }
}
```

### "1콜"의 의미

위 표에서 "호출 1회로 집계"가 예인 도구를 **한 번 실행하면 1**이다.
검색 결과가 비어 있거나 정적 카탈로그에서 나왔거나, 키 확인 뒤에 네이버 API가 실패해도 1로 친다.
품목신고 조회도 같다. `FOODSAFETYKOREA_API_KEY`가 없거나 공식 API가 실패해도, 호출자 키가 유효하면 1로 친다.
한도를 넘긴 호출, 키가 없거나 틀린 호출, `get_usage`, `GET /health`, `GET /usage`는 세지 않는다.
한도는 UTC 달력 월이고, 다음 달 1일 00:00:00 UTC에 그 키의 카운터가 0부터 다시 시작한다.

기본 한도는 `MCP_MONTHLY_QUOTA` (생략 시 1000).
특정 키만 바꾸려면 `MCP_KEY_QUOTAS=dev-demo-key:100,partner-key:5000`.

한도를 넘기면 도구 결과는 `isError`이고, 본문은 예를 들면 다음과 같다.

```json
{
  "error": "quota_exceeded",
  "message": "이번 달 호출 한도를 모두 사용했습니다. 초기화 시각 이후에 다시 호출할 수 있습니다.",
  "calls_used": 1000,
  "calls_remaining": 0,
  "quota": 1000,
  "period": "2026-10",
  "resets_at": "2026-11-01T00:00:00Z"
}
```

### 사용량 확인

- MCP 도구 `get_usage` — 이 키의 집계만 반환하고, 호출 수에 포함되지 않는다.
- `GET /usage` — 같은 내용. 키는 헤더 또는 `?api_key=`. 다른 키의 숫자는 없다.
- `GET /health` — `{"status":"ok","service":"agent-shopping"}`. 사용량 없음.

사용량은 서버 로컬 SQLite (`usage.db`, 경로는 `MCP_USAGE_DB_PATH`)에 키의 SHA-256만 저장한다.
Render 무료 웹 서비스 디스크는 재배포·재시작 때 비워지므로, 그때 카운터도 초기화된다.

### Render 환경변수

대시보드 → 서비스 → Environment. 값을 바꾼 뒤에는 서비스를 다시 배포하거나 재시작한다.

| 변수 | 예 | 설명 |
|---|---|---|
| `MCP_API_KEYS` | `dev-demo-key,partner-key` | 허용 키. 비어 있으면 도구 호출이 `server_unconfigured`로 실패 |
| `MCP_MONTHLY_QUOTA` | `1000` | 키별 덮어쓰기가 없는 키의 월 한도 |
| `MCP_KEY_QUOTAS` | `dev-demo-key:100` | `키:한도`. 한도는 마지막 `:` 뒤의 정수 |
| `MCP_USAGE_DB_PATH` | `/var/data/usage.db` | 생략 시 앱 디렉터리의 `usage.db` |
| `MCP_TRANSPORT` | `streamable-http` | 기존과 동일. Start Command에 이미 있으면 생략 가능 |
| `FOODSAFETYKOREA_API_KEY` | (식품안전나라에서 발급) | I0030 인증키. 비우면 품목신고 조회만 `filing_api_unconfigured` |

`MCP_API_KEY`는 Render에 넣지 않는다. stdio 전용이고, HTTP 호출자를 인증하지 않는다.
PlayMCP 시험에 예시 키를 쓸 거면 `MCP_API_KEYS`에 `dev-demo-key`를 넣고,
그 키의 한도를 `MCP_KEY_QUOTAS`로 낮게 잡는 편이 안전하다.

## 실행

```bash
cd ~/workspace/agent-shopping-mcp
cp .env.example .env          # 로컬 키 dev-demo-key. 이미 있는 환경변수는 덮어쓰지 않음
.venv/bin/python -m unittest tests.test_metering
.venv/bin/python server.py    # stdio 모드 (MCP 클라이언트용). 같은 프로세스에 MCP_API_KEY 필요
```

## 실전 데이터 연결 (네이버 쇼핑 API, 무료)

1. https://developers.naver.com → 애플리케이션 등록 → 검색 API 사용 승인
2. `.env.example`을 복사해 `.env`를 만들고 네이버 값을 채운다.
   미터링 키(`MCP_API_KEYS`, `MCP_API_KEY`)도 이 파일에 있다.
   ```
   NAVER_CLIENT_ID=발급받은ID
   NAVER_CLIENT_SECRET=발급받은Secret
   AFFILIATE_ID=쿠팡파트너스ID (선택)
   ```
3. 키가 있으면 네이버 쇼핑 검색을 쓴다. 키가 없으면 아래 정적 카탈로그로 동작한다.

## 정적 카탈로그 (네이버 키 없을 때)

네이버 쇼핑 검색 API는 새로 발급되지 않는다. `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET`이
없으면 `search_products`는 루트 `catalog.json`의 영양제·펫푸드를 상품명·브랜드·키워드로
찾고 가격이 낮은 순으로 돌려준다. `find_pet_food`와 `compare_nutrition`도 같은 파일의
펫푸드만 본다. 응답 출처는 `static_catalog (수동 갱신 YYYY-MM-DD)`이고, 실시간 검색이 아니다.
맞는 상품이 없으면 다른 상품으로 채우지 않는다.

검색 결과의 가격 기준일은 카탈로그 `checked_at`이다. 조회한 시각이 그 가격의 기준일이 되지는 않는다.
상품 URL은 카탈로그 `url`이 판매 상품 페이지일 때만 나온다. 쿠팡 브랜드 목록(`/np/products/brand-shop`)과
로우차트·퍼니즈 가격 추적 주소는 상품 URL로 주지 않고 `상품 URL: 없음`이라고 적는다.
`get_buy_link`도 그 판매 상품 페이지가 카탈로그에 있을 때만 같은 주소를 돌려준다.
어필리에이트 태그는 그렇게 확인된 `coupang.com` 상품 페이지에만 붙는다. 없는 주소는 만들지 않는다.

현재 파일은 2026-10-25 기준 80개다. 영양제 37, 강아지 사료 23, 고양이 사료 20.
항목을 넣고 빼면 이 개수도 같이 고친다.

가격을 고칠 때:

1. 쿠팡 브랜드샵, 핏펫, 로우차트, 퍼니즈 등에서 그 상품의 현재 판매가를 페이지에서 확인한다.
2. 확인한 상품만 `price`, `mall`, `url`, `price_source_url`, `checked_at`(본 날짜)을 고친다. 파일 상단 `checked_at`도 맞춘다.
3. 가격을 못 본 상품은 넣지 않거나 뺀다. 가격은 추측하지 않는다.
4. `python -m json.tool catalog.json`으로 확인한 뒤 서버를 다시 실행한다. 카탈로그는 기동 시 한 번 읽는다.
5. `category`는 `supplement` 또는 `pet_food`만. 전자제품처럼 이 버티컬 밖 상품은 넣지 않는다.
6. 항목 수가 바뀌면 위 개수(영양제·강아지 사료·고양이 사료)도 같이 고친다.

## 건강기능식품 품목신고 조회

`lookup_functional_food_filing`은 식품의약품안전처 식품안전나라 OpenAPI `I0030`
(건강기능식품 품목제조 신고사항 현황)을 품목제조번호로만 조회한다.
브랜드나 제품명으로는 찾지 않는다. 번호는 문자열로 비교하고 앞의 0을 지우지 않는다.

이 자료에는 가격과 구매 주소가 없다. 도구도 그 둘을 만들지 않는다.
응답의 기능성 문구는 신고서에 적힌 문장이다. 효과 보증이나 복용 적합 판단이 아니고,
품목 신고가 식약처의 추천이라는 뜻도 아니다.

2026-11-08에 확인한 공개 문서:

- 데이터 목록: https://www.data.go.kr/data/15061756/openapi.do
  포털 표기는 비용 무료, 이용허락범위 제한 없음이다.
  이 문장은 이용허락 표시이지 호출 횟수 제한이 없다는 뜻이 아니다.
- 필드·요청 형식·호출 제한: https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=I0030
  인증키와 서비스 활용신청이 필요하다.
  문서에는 증분 파라미터 없이 시간당 100회, 06시~19시 인증키·서비스별 300회,
  파일 다운로드는 하루 10회가 적혀 있다. 상단의 `API호출제한 1000`은 기간이 적혀 있지 않다.
- 신청 절차: https://www.foodsafetykorea.go.kr/api/howToUseApi.do
  신청 화면에서 공공데이터활용 이용약관에 동의한다. 약관 전문은 이 저장소에 옮기지 않았다.

호출은 `https://openapi.foodsafetykorea.go.kr` 로만 나간다.
인증키는 그 주소의 경로에 들어가므로, 오류 문구와 로그에 요청 주소 전체를 남기지 않는다.

키가 없을 때의 동작은 고정이다. `FOODSAFETYKOREA_API_KEY`가 비어 있으면
`filing_api_unconfigured`를 반환하고 신고 내용을 만들지 않는다.
이 호출은 다른 과금 도구와 같이 월간 1회로 센다. 키가 없거나 공식 API가 실패해도,
호출자 키가 유효하면 1회다. `get_usage`의 `metered_tools`에 이 도구 이름이 들어간다.

### 사용자가 키를 넣는 절차

1. https://www.foodsafetykorea.go.kr 에서 회원가입하고 로그인한다.
2. 데이터 공개에서 `건강기능식품 품목제조 신고사항 현황` (`I0030`)을 고르고 Open-API 이용신청을 한다.
   서비스 설명: https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=I0030
3. 이용약관에 동의하고 활용목적을 입력한다. 안내는 https://www.foodsafetykorea.go.kr/api/howToUseApi.do
4. 인증키는 신청 후 자동 발급된다. 인증키 신청 현황에서 본인 키를 확인한다.
5. Render Environment에 이름 `FOODSAFETYKOREA_API_KEY`, 값으로 그 키만 넣고 서비스를 다시 시작한다.
   키 값은 저장소, 로그, 도구 응답에 넣지 않는다.

공개 샘플 키로 확인한 조회 동작(2026-11-08, 샘플 키는 운영 키가 아니다):
`PRDLST_REPORT_NO`에 전체 번호를 넣으면 그 번호 1건이 나왔고,
앞부분만 넣거나 앞에 0을 붙인 값은 `INFO-200`(해당 데이터 없음)이었다.
업소명 `BSSH_NM`은 일부 글자만으로도 여러 건이 나왔으므로 이 도구는 그 조건을 쓰지 않는다.

## 배포 (남의 에이전트가 쓰게 하려면)

MCP 서버는 기본적으로 내 PC에서만 돈다. 남의 에이전트가 쓰려면 인터넷에 올려야 함.

1. **로컬 테스트**: Claude Desktop 설정에 등록
   ```json
   { "mcpServers": { "agent-shopping":
     { "command": "/home/hatch/workspace/agent-shopping-mcp/.venv/bin/python",
       "args": ["/home/hatch/workspace/agent-shopping-mcp/server.py"] } } }
   ```
2. **원격 배포**: Render/Railway/Oracle Cloud 무료 티어에 올리고
   Streamable HTTP로 노출 → URL만 있으면 어떤 에이전트든 접속 가능
3. **카카오 PlayMCP 등록**: 카카오톡 카나나 에이전트가 쓰는 외부 MCP 마켓.
   등록되면 카톡 유저들의 에이전트가 네 서버를 호출할 수 있음
   (한국에서 가장 현실적인 유통 채널)

## 돈 버는 구조

```
에이전트 → 네 MCP 서버 → 상품 검색/가격 비교 → 구매 링크 클릭 → 결제
                                                        ↓
                                              어필리에이트 수수료 정산
                                              (쿠팡 파트너스 ~3% 등)
```

- 서버 자체는 무료. 돈은 **구매 링크 경유 수수료**에서 나온다.
- 에이전트 경유 구매가 늘어날수록 수수료가 느는 구조 = 통행료
- 다음 단계: 가격 히스토리가 쌓이면 `check_price_timing`이
  "지금 사라"는 타이밍 조언의 근거가 되어 에이전트가 네 서버를 쓸 이유가 생김
