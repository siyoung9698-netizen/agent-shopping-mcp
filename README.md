# 에이전트용 최저가+타이밍 쇼핑 MCP (MVP)

AI 에이전트가 호출하는 쇼핑 도구 서버. 에이전트가 이 서버를 경유해 상품을 팔면
어필리에이트 수수료가 발생한다.

## 도구

| 도구 | 하는 일 | 호출 1회로 집계 |
|---|---|---|
| `search_products` | 상품 검색 → 최저가순 정렬. 호출될 때마다 가격을 DB에 기록 | 예 |
| `check_price_timing` | 90일 가격 히스토리와 비교해 "지금 살 때인지" 판단 | 예 |
| `get_buy_link` | 어필리에이트 태그가 붙은 구매 링크 반환 | 예 |
| `find_pet_food` | 강아지/고양이 사료를 조건으로 검색 | 예 |
| `compare_nutrition` | 두 사료의 영양성분을 비교 | 예 |
| `get_usage` | 이 API 키의 이번 달 사용량·남은 한도·초기화 시각 | 아니오 |

## API 키와 호출 미터링

도구를 호출하려면 API 키가 필요하다. 키 없는 호출은 빈 결과가 아니라
`missing_api_key` / `invalid_api_key` / `quota_exceeded` 같은 구조화된 오류로 거절된다.
`initialize`와 도구 목록 조회, `GET /health`는 키 없이 된다.
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
검색 결과가 비어 있거나 목업이거나, 키 확인 뒤에 네이버 API가 실패해도 1로 친다.
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
3. 네이버 키가 없으면 목업 카탈로그로 동작 (개발/테스트용)

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
