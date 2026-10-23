-- 프로필·구매 기록 테이블 뼈대 — DRAFT (토요일 킥오프용)
-- 목적: MOAT.md 9절 장벽 C — "구매 폐쇄루프 + 개인 프로필 축적(전환비용)".
-- 등록하는 서버에 뼈대만 넣어두고, 실제 입력 UI/도구는 트래픽 붙은 뒤에 붙인다.
-- 적용: sqlite3 prices.db < drafts/schema_v2.sql

-- 1) 프로필: 한 번 입력하면 다른 MCP로 옮길 때 다시 입력해야 하는 정보 = 전환비용
CREATE TABLE IF NOT EXISTS profiles(
  profile_id   TEXT PRIMARY KEY,          -- 에이전트/유저 식별자 (해시 권장)
  created_ts   TEXT NOT NULL,
  updated_ts   TEXT NOT NULL,
  -- 반려동물 (펫 버티컬 1순위)
  pet_type     TEXT,                      -- 강아지 | 고양이
  pet_breed    TEXT,                      -- 품종
  pet_age_years REAL,                     -- 나이
  pet_weight_kg REAL,                     -- 체중 (사료 급여량 계산용)
  pet_allergies TEXT,                     -- 쉼표 구분 알러지 원료
  -- 가구 (렌탈·가전 버티컬용)
  household_size INTEGER,
  address_region TEXT,                    -- 시/도 수준만 (정밀 주소 저장 금지)
  -- 렌탈 (고단가 축)
  rental_contracts TEXT,                  -- JSON: [{품목, 업체, 만료일, 월액}] — 만료 알림의 재료
  notes        TEXT
);

-- 2) 구매 기록: '검색→추천→실제 구매→만족/반품' 폐쇄루프의 그릇
--    스크래핑으로 절대 못 만드는 유일한 데이터.
CREATE TABLE IF NOT EXISTS purchases(
  purchase_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  profile_id   TEXT,                      -- profiles.profile_id (없으면 NULL)
  product_key  TEXT NOT NULL,             -- price_history와 같은 키 규칙
  name         TEXT,
  price        INTEGER,                   -- 실제 결제액
  mall         TEXT,
  url          TEXT,
  purchased_ts TEXT NOT NULL,
  returned     INTEGER DEFAULT 0,         -- 0=유지, 1=반품됨
  satisfaction INTEGER,                  -- 1~5 (나중에 수집)
  FOREIGN KEY(profile_id) REFERENCES profiles(profile_id)
);
CREATE INDEX IF NOT EXISTS idx_purchases_pkey ON purchases(product_key);
CREATE INDEX IF NOT EXISTS idx_purchases_profile ON purchases(profile_id);
