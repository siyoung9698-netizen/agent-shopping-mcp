"""4개 버티컬 통합 카탈로그 스키마

버티컬: pet (펫) / supplements (영양제) / cosmetics (화장품) / camping (캠핑·등산)
원칙: 에이전트가 숫자로 판단할 수 있는 지표 필드만 둔다.
"""

# ---------- 공통 필드 (전 버티컬) ----------
COMMON_FIELDS = {
    "product_id": "고유 ID (브랜드+상품명 해시)",
    "name": "상품명",
    "brand": "브랜드",
    "price": "현재가 (원, int)",
    "mall": "판매처",
    "url": "상품 URL",
    "image_url": "이미지 URL",
    "rating": "평점 (float)",
    "review_count": "리뷰 수 (int)",
    # 가격 히스토리는 별도 테이블(price_history)에 product_id 기준으로 쌓임
}

# ---------- 버티컬별 지표 필드 ----------
VERTICAL_SCHEMAS = {
    "pet": {
        "label": "펫 (사료·간식)",
        "fields": {
            "pet_type": "강아지 | 고양이",
            "life_stage": "퍼피/키튼 | 어덜트 | 시니어 | 전연령",
            "size": "소형 | 중형 | 대형 (강아지)",
            "protein_pct": "조단백 % (float) ← 핵심 지표",
            "fat_pct": "조지방 % (float)",
            "kcal_per_kg": "kcal/kg (int)",
            "main_ingredients": "주원료 상위 3개 (list)",
            "grain_free": "그레인프리 여부 (bool)",
            "allergens": "알러지 유발 원료 (list)",
        },
        "derived": {"price_per_kg": "price / 중량(kg) — 단위가격 비교용"},
        "sort_keys": ["protein_pct desc", "price asc", "rating desc"],
        "filters": ["pet_type", "life_stage", "grain_free", "exclude_allergen", "max_price"],
        "mcp_tools": ["find_pet_food", "compare_nutrition"],
    },
    "supplements": {
        "label": "영양제",
        "fields": {
            "ingredient": "주성분 (예: 오메가3, 루테인)",
            "dosage": "1회 함량 (예: 1000mg)",
            "daily_dose": "1일 섭취량 (정/캡슐 수)",
            "servings": "총 섭취 일수 (정수)",
            "function": "기능성 (눈/간/관절/면역/수면...)",
            "certified": "건강기능식품 인증 여부 (bool)",
        },
        "derived": {"price_per_day": "price / servings — 1일당 가격, 핵심 비교 지표"},
        "sort_keys": ["price_per_day asc", "dosage desc", "rating desc"],
        "filters": ["function", "ingredient", "certified", "max_price_per_day"],
        "mcp_tools": ["find_supplement", "compare_supplement"],
    },
    "cosmetics": {
        "label": "화장품",
        "fields": {
            "category": "스킨케어 | 선크림 | 클렌징 | 마스크팩 ...",
            "skin_type": "지성 | 건성 | 복합성 | 민감성 | 전타입",
            "volume_ml": "용량 (ml/g)",
            "key_ingredients": "핵심 성분 (list, 예: 나이아신아마이드, 히알루론산)",
            "exclude_ingredients": "주의 성분 (향료, 알코올...)",
            "spf": "SPF (선크림)",
        },
        "derived": {"price_per_ml": "price / volume_ml — 용량당 가격, 핵심 비교 지표"},
        "sort_keys": ["price_per_ml asc", "rating desc", "review_count desc"],
        "filters": ["category", "skin_type", "key_ingredient", "max_price"],
        "mcp_tools": ["find_cosmetic", "compare_cosmetic"],
    },
    "camping": {
        "label": "캠핑·등산 장비",
        "fields": {
            "category": "텐트 | 침낭 | 백팩 | 버너 | 체어 ...",
            "weight_g": "무게 (g, int) ← 핵심 지표",
            # 카테고리별 스펙 (해당될 때만)
            "waterproof_mm": "내수압 (mm, 텐트/자켓)",
            "capacity": "수용 인원 (텐트)",
            "comfort_temp": "쾌적온도 ℃ (침낭)",
            "limit_temp": "한계온도 ℃ (침낭)",
            "volume_l": "용량 (L, 백팩)",
        },
        "derived": {"spec_per_weight": "카테고리별 스펙 ÷ 무게 — 가성비의 정량화"},
        "sort_keys": ["weight_g asc", "rating desc", "price asc"],
        "filters": ["category", "max_weight_g", "min_waterproof_mm", "max_price"],
        "mcp_tools": ["find_camping_gear", "compare_camping_spec"],
    },
}

# ---------- 가격 히스토리 테이블 (공통) ----------
# price_history(product_id, price, mall, ts)
# 매일 크롤러가 수집 → check_price_timing의 근거 데이터
# 30일치 = "한 달 추이", 90일치 = "타이밍 엔진 완성"
