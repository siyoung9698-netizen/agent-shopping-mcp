# 에이전트용 최저가+타이밍 쇼핑 MCP (MVP)

AI 에이전트가 호출하는 쇼핑 도구 서버. 에이전트가 이 서버를 경유해 상품을 팔면
어필리에이트 수수료가 발생한다.

## 도구 3개

| 도구 | 하는 일 |
|---|---|
| `search_products` | 상품 검색 → 최저가순 정렬. 호출될 때마다 가격을 DB에 기록 |
| `check_price_timing` | 90일 가격 히스토리와 비교해 "지금 살 때인지" 판단 |
| `get_buy_link` | 어필리에이트 태그가 붙은 구매 링크 반환 |

## 실행

```bash
cd ~/workspace/agent-shopping-mcp
.venv/bin/python server.py   # stdio 모드 (MCP 클라이언트용)
```

## 실전 데이터 연결 (네이버 쇼핑 API, 무료)

1. https://developers.naver.com → 애플리케이션 등록 → 검색 API 사용 승인
2. `.env` 파일 생성:
   ```
   NAVER_CLIENT_ID=발급받은ID
   NAVER_CLIENT_SECRET=발급받은Secret
   AFFILIATE_ID=쿠팡파트너스ID (선택)
   ```
3. 키가 없으면 목업 카탈로그로 동작 (개발/테스트용)

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
