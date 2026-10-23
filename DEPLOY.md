# 원격 배포 + PlayMCP 등록 가이드

## 0. server.py 수정 내역 (2026-10-20, 백업: server.py.bak_20261020)

두 가지를 고쳤다. 둘 다 검증 완료.

**① 펫 버티컬 도구 미등록 버그 수정**
- `if __name__ == "__main__": mcp.run()` 이 파일 중간에 있어서,
  `mcp.run()`이 블로킹되는 바람에 그 아래에 정의된
  `find_pet_food`, `compare_nutrition`이 절대 등록되지 않았음.
- `__main__` 블록을 파일 맨 끝으로 이동 → 5개 도구 전부 등록 확인.

**② 원격 배포용 transport 추가 (기존 로컬 동작 그대로)**
- 기본값은 그대로 stdio (로컬 MCP 클라이언트용).
- `MCP_TRANSPORT=streamable-http` 환경변수로 HTTP 모드 전환.
- HTTP 모드에서는 `PORT` / `HOST` 환경변수로 포트·바인딩 주소 지정
  (기본 0.0.0.0:8000 — Render의 $PORT 자동 대응).
  - 주의: FastMCP 생성자 기본값(host 127.0.0.1, port 8000)이
    환경변수보다 우선 적용되는 라이브러리 동작 때문에,
    코드에서 `mcp.settings`를 직접 덮어쓰는 방식으로 해결함.

## 1. Render 배포 (무료, 카드 불필요)

| 항목 | 내용 |
|---|---|
| 요금 | $0 — 월 750시간, 카드 등록 불필요 |
| 조건 | 15분 무응답 시 슬립, 첫 호출에 약 1분 소요 |
| 대역폭 | 월 5GB (이 용도로는 충분) |
| DB | prices.db(SQLite)는 재배포/재시작 시 초기화됨 — MVP 단계에서는 감수 |

**배포 순서**
1. GitHub에 이 폴더 push (prices.db, .bak 파일, __pycache__ 제외)
2. Render → New → Web Service → 리포지토리 연결
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `MCP_TRANSPORT=streamable-http python server.py`
5. Environment Variables:
   - `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` (네이버 개발자센터에서 검색 API 발급)
   - `AFFILIATE_ID` (쿠팡 파트너스 ID — 나중에)
6. 배포 후 `https://<서비스명>.onrender.com/mcp` 가 MCP 엔드포인트

**슬립 이슈**: PlayMCP 경유 첫 호출이 1분 걸리면 타임아웃 날 수 있음.
MVP 테스트 단계에서는 감수하고, 실제 트래픽 붙으면
Starter($7/월, 상시 가동)로 올리는 게 정석.

**Railway는 제외**: $5 일회성 크레딧 후 월 $1 — 진짜 무료 티어 아님.

## 2. PlayMCP 등록 (카카오)

- 개발자 콘솔: https://playmcp.kakao.com/console
- 콘솔에서 MCP 서버 등록 → **심사 통과해야 공개 목록에 노출**
- 심사 기준: kko.kakao.com/playmcp_review / 문의: 카카오 디스코드 채널
- 등록할 때 필요한 것: 위 Render URL (`/mcp` 경로)

**발견(discovery) 문제 — 꼭 기억**
- 심사를 통과해 목록에 올라가도 끝이 아님.
- 카카오톡 카나나/클로드 유저가 **직접 '도구함'에 담아야** 호출됨.
- 즉, "올리면 자동으로 쓰인다"가 아니라 "유저가 담게 만들어야 한다"가 진짜 과제.
- 초기 유통 아이디어: 지인·GC 그룹 테스트 → 사용 후기 → PlayMCP 내 평점/리뷰.

## 4. PlayMCP 자체 호스팅 옵션 — Render 대안 (2026-10-20 확인)

**발견**: PlayMCP 콘솔에는 "Git 소스 빌드" 방식이 있다. GitHub URL을 넣으면
카카오가 직접 MCP 서버를 빌드·배포해주고, 엔드포인트 URL을 발급한다.
즉, Render(무료 티어 슬립 문제)를 거치지 않고 PlayMCP에 바로 올릴 수 있다.
(출처: 2026-07 Medium 등록 실습기 — https://medium.com/@SeoDDuu/mcp-서버-등록해서-직접-사용해보기-be6909cff89d)

- 조건/쿼터는 미확인 — 콘솔에서 직접 확인 필요.
- Render 경로(위 1절)는 그대로 유효한 대안. 둘 중 편한 쪽 선택.

**반드시 '임시 등록'부터 (심사 요청 금지)**
- 콘솔에서 "등록 및 심사 요청"이 아니라 **임시 등록**으로 먼저 올린다.
- 임시 등록한 서버를 '도구함'에 추가 → PlayMCP 내장 **AI 채팅**으로
  도구가 정상 호출되는지 테스트 (하루 질문 한도 있음).
- 충분히 테스트한 뒤에 심사 요청. 심사 기준: kko.kakao.com/playmcp_review
  (내용은 콘솔 로그인 후 확인). 운영 문의: 카카오 디스코드
  (kko.kakao.com/playmcp_discord).

**발견(discovery) 메커니즘 — 한 번 더**
- 공개 목록에 올라가도, 유저가 직접 '도구함'에 담아야만 호출된다.
- 클로드 커넥터는 유료 플랜에서만 열림(클로드 정책), 챗GPT는 개발자 모드에서
  커스텀 URL 등록. GC 그룹 테스트는 PlayMCP 내장 AI 채팅(카카오 로그인만)이
  가장 마찰이 적다. (출처: worlddrop.app PlayMCP 사용법, 2026-10-16 확인 —
  https://worlddrop.app/s/playmcp-kakao.html)

## 5. 남은 결정 (사용자 몫)

- [ ] GitHub 리포지토리 생성 + push (내가 도와줄 수 있음)
- [ ] Render 계정 생성 (GitHub 로그인으로 1분) — PlayMCP 자체 호스팅 쓰면 생략 가능
- [ ] 네이버 검색 API 키 발급 (developers.naver.com, 무료)
- [ ] 쿠팡 파트너스 가입 (수익화 시점)
- [ ] PlayMCP 콘솔 등록 (카카오 로그인 필요 — 본인이 직접) — 임시 등록 → AI 채팅 테스트 → 심사 요청 순서
