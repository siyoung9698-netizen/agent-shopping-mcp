"""Exact lookup of one MFDS health functional food filing (I0030).

The request goes only to https://openapi.foodsafetykorea.go.kr.
The API key stays in the path of that request and is not copied into errors.
Fixtures belong in tests. This module does not invent a filing.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ENV_NAME = "FOODSAFETYKOREA_API_KEY"
OFFICIAL_HOST = "openapi.foodsafetykorea.go.kr"
SERVICE_ID = "I0030"
PAGE_START = 1
PAGE_END = 5
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 350_000
MAX_FIELD_CHARS = 500
DOC_URL = "https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=I0030"
DATASET_URL = "https://www.data.go.kr/data/15061756/openapi.do"

DISCLAIMER = (
    "이 값은 건강기능식품 품목제조 신고 자료다. "
    "식약처가 효과를 보증하거나 특정인에게 맞다고 정한 것이 아니다. "
    "가격과 구매 주소는 이 자료에 없다."
)

SOURCE = {
    "provider": "식품의약품안전처",
    "service_id": SERVICE_ID,
    "service_name": "건강기능식품 품목제조 신고사항 현황",
    "documentation_url": DOC_URL,
    "dataset_url": DATASET_URL,
    "use_permission": (
        "공공데이터포털은 2026-11-08 확인 기준으로 이 데이터셋을 무료, "
        "이용허락범위 제한 없음으로 표시한다. 이 표시는 호출 한도가 없다는 뜻이 아니고, "
        "인증키와 I0030 문서의 호출 제한이 적용된다."
    ),
}

# Official response names verified against the I0030 field list and a sample JSON body.
_FIELDS = (
    ("report_no", "PRDLST_REPORT_NO"),
    ("product_name", "PRDLST_NM"),
    ("company_name", "BSSH_NM"),
    ("license_no", "LCNS_NO"),
    ("permitted_on", "PRMS_DT"),
    ("last_updated", "LAST_UPDT_DTM"),
    ("shelf_life", "POG_DAYCNT"),
    ("appearance", "DISPOS"),
    ("form", "PRDT_SHAP_CD_NM"),
    ("intake", "NTK_MTHD"),
    ("filed_claim_text", "PRIMARY_FNCLTY"),
    ("cautions", "IFTKN_ATNT_MATR_CN"),
    ("storage", "CSTDY_MTHD"),
    ("product_type", "PRDLST_CDNM"),
    ("specification", "STDR_STND"),
    ("functional_ingredients", "INDIV_RAWMTRL_NM"),
    ("other_ingredients", "ETC_RAWMTRL_NM"),
    ("capsule_ingredients", "CAP_RAWMTRL_NM"),
    ("indicator_ingredients", "RAWMTRL_NM"),
    ("production_ended", "PRODUCTION"),
    ("high_calorie_low_nutrient", "HIENG_LNTRT_DVS_NM"),
    ("child_preference_certified", "CHILD_CRTFC_YN"),
    ("packaging_material", "FRMLC_MTRQLT"),
    ("packaging_method", "FRMLC_MTHD"),
    ("business_type", "INDUTY_CD_NM"),
)

_AUTH_CODES = {"INFO-100", "INFO-110", "INFO-120", "INFO-400", "INFO-600"}
_QUOTA_CODES = {"INFO-300", "INFO-310", "INFO-320", "INFO-330", "ERROR-503"}
_API_URL_RE = re.compile(r"https?://openapi\.foodsafetykorea\.go\.kr/[^\s\"\\<>,]+")


class FilingError(Exception):
    """A caller-facing filing failure that must not carry the request URL."""

    def __init__(self, code: str, message: str, **extra: object):
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(message)


def unconfigured_error() -> FilingError:
    return FilingError(
        "filing_api_unconfigured",
        f"{ENV_NAME}가 비어 있다. 식품안전나라에서 I0030 인증키를 발급한 뒤 그 환경변수에만 넣는다. "
        "신고 내용을 만들지 않는다.",
    )


def validate_report_no(value: str) -> str:
    """Keep the identifier as text, including leading zeroes. Reject names."""
    if not isinstance(value, str):
        raise FilingError(
            "invalid_identifier",
            "품목제조번호는 문자열로 된 숫자여야 한다. 브랜드명으로는 찾지 않는다.",
        )
    text = value.strip()
    if not text.isdigit() or not 1 <= len(text) <= 32:
        raise FilingError(
            "invalid_identifier",
            "품목제조번호는 앞뒤 공백을 뺀 1~32자리 숫자여야 한다. 앞의 0은 유지한다. 브랜드명으로는 찾지 않는다.",
        )
    return text


def validate_api_key(value: str) -> str:
    text = value.strip()
    if not text or any(ch.isspace() for ch in text) or len(text) > 200:
        raise unconfigured_error()
    return text


def build_request_url(report_no: str, api_key: str) -> str:
    """One exact-number request. The key is a single encoded path segment."""
    report_no = validate_report_no(report_no)
    api_key = validate_api_key(api_key)
    key = urllib.parse.quote(api_key, safe="")
    report = urllib.parse.quote(report_no, safe="")
    return (
        f"https://{OFFICIAL_HOST}/api/{key}/{SERVICE_ID}/json/"
        f"{PAGE_START}/{PAGE_END}/PRDLST_REPORT_NO={report}"
    )


def ensure_official_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host != OFFICIAL_HOST:
        raise FilingError("upstream_error", "공식 API 호스트가 아니라 호출을 중단했다.")
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) != 7
        or parts[0] != "api"
        or parts[2] != SERVICE_ID
        or parts[3] != "json"
        or parts[4] != str(PAGE_START)
        or parts[5] != str(PAGE_END)
    ):
        raise FilingError("upstream_error", "공식 API 경로가 아니라 호출을 중단했다.")
    last = urllib.parse.unquote(parts[6])
    if not last.startswith("PRDLST_REPORT_NO=") or not last.split("=", 1)[1].isdigit():
        raise FilingError("upstream_error", "품목제조번호 외의 조건은 호출하지 않는다.")
    if parsed.query or "BSSH_NM=" in url or "PRDLST_NM=" in url:
        raise FilingError("upstream_error", "업소명이나 품목명 조건은 호출하지 않는다.")


class OfficialRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            ensure_official_url(newurl)
        except FilingError as exc:
            raise urllib.error.URLError("non-official redirect") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def interpret_response(body: object, report_no: str, queried_at: str) -> dict:
    """Keep only rows whose 품목제조번호 equals report_no as text."""
    if not isinstance(body, dict):
        raise FilingError("upstream_error", "공식 API 응답이 JSON 객체가 아니다.")
    block = body.get(SERVICE_ID)
    if not isinstance(block, dict):
        raise FilingError("upstream_error", "공식 API 응답에 I0030 결과가 없다.")
    result = block.get("RESULT") if isinstance(block.get("RESULT"), dict) else {}
    code = result.get("CODE") if isinstance(result.get("CODE"), str) else ""
    if code in _AUTH_CODES:
        raise FilingError("upstream_auth", "공식 API가 인증키 또는 활용신청을 거부했다.", upstream_code=code)
    if code in _QUOTA_CODES:
        raise FilingError("upstream_quota", "공식 API 호출 한도에 걸렸다.", upstream_code=code)
    if code and code not in {"INFO-000", "INFO-200"}:
        raise FilingError("upstream_error", "공식 API가 오류 코드를 반환했다.", upstream_code=code)

    rows = _rows(block.get("row"))
    exact = [row for row in rows if _report_no_of(row) == report_no]
    total = _count(block.get("total_count"))
    base = _base(report_no, queried_at, code or None, total)
    if code == "INFO-200" or (not rows and not exact):
        base["status"] = "no_exact_match"
        base["message"] = "같은 품목제조번호의 신고가 없다."
        return base
    if not exact:
        if total is not None and total > len(rows):
            base["status"] = "inconclusive"
            base["message"] = (
                "받아 온 행 안에는 같은 품목제조번호가 없고, 응답하지 않은 행이 더 있어 없음으로 단정하지 않는다."
            )
        else:
            base["status"] = "no_exact_match"
            base["message"] = "응답 행의 품목제조번호가 요청 값과 같지 않다."
        return base

    filings = [_filing(row, report_no) for row in exact[:PAGE_END]]
    missing: list[str] = []
    for filing in filings:
        for key, value in filing.items():
            if value is None and key not in missing:
                missing.append(key)
    base["filings"] = filings
    base["missing_fields"] = missing
    base["upstream_code"] = code or "INFO-000"
    if len(exact) > 1 or (total is not None and total > len(exact)):
        base["status"] = "multiple"
        base["message"] = "같은 품목제조번호의 신고가 둘 이상이다. 하나를 골라 담지 않는다."
        if total is not None and total > len(filings):
            base["truncated"] = True
    else:
        base["status"] = "ok"
        base["message"] = "품목제조번호가 같은 신고 1건이다."
    return _fit(base)


def fetch_i0030(report_no: str, api_key: str, *, opener=None) -> dict:
    report_no = validate_report_no(report_no)
    api_key = validate_api_key(api_key)
    url = build_request_url(report_no, api_key)
    ensure_official_url(url)
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "agent-shopping-mcp"})
    open_url = opener or _open_official
    try:
        with open_url(request, timeout=TIMEOUT_SECONDS) as response:
            final_url = response.geturl() if hasattr(response, "geturl") else url
            ensure_official_url(final_url)
            raw = _read_limited(response, MAX_RESPONSE_BYTES)
    except FilingError:
        raise
    except TimeoutError:
        raise FilingError("upstream_timeout", "공식 API 응답이 제한 시간 안에 오지 않았다.", timeout_seconds=TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        raise FilingError("upstream_error", f"공식 API가 HTTP {exc.code}를 반환했다.")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError) or "timed out" in str(getattr(exc, "reason", "")).lower():
            raise FilingError(
                "upstream_timeout",
                "공식 API 응답이 제한 시간 안에 오지 않았다.",
                timeout_seconds=TIMEOUT_SECONDS,
            )
        raise FilingError("upstream_error", "공식 API에 연결하지 못했다.")
    except json.JSONDecodeError:
        raise FilingError("upstream_error", "공식 API 응답이 JSON이 아니다.")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise FilingError("upstream_error", "공식 API 응답이 JSON이 아니다.")
    queried_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = interpret_response(body, report_no, queried_at)
    return _redact(payload, api_key)


def error_payload(err: FilingError, report_no: str | None = None) -> dict:
    payload = _base(report_no, None, err.extra.get("upstream_code") if isinstance(err.extra.get("upstream_code"), str) else None, None)
    payload["status"] = "error"
    payload["error"] = err.code
    payload["message"] = _redact_text(err.message, "")
    if isinstance(err.extra.get("timeout_seconds"), int):
        payload["timeout_seconds"] = err.extra["timeout_seconds"]
    if report_no is None and isinstance(err.extra.get("report_no"), str):
        payload["report_no"] = err.extra["report_no"]
    return payload


def _open_official(request, timeout):
    opener = urllib.request.build_opener(OfficialRedirectHandler)
    return opener.open(request, timeout=timeout)


def _read_limited(response, limit: int) -> bytes:
    chunks = []
    total = 0
    while True:
        block = response.read(8192)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise FilingError("upstream_error", "공식 API 응답이 허용 크기를 넘었다.")
        chunks.append(block)
    return b"".join(chunks)


def _rows(value) -> list[dict]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    return []


def _report_no_of(row: dict):
    value = row.get("PRDLST_REPORT_NO")
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _count(value):
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _filing(row: dict, report_no: str) -> dict:
    filing = {}
    for name, source in _FIELDS:
        if name == "report_no":
            filing[name] = report_no
            continue
        filing[name] = _field_text(row.get(source))
    return filing


def _field_text(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    if not text:
        return None
    if len(text) > MAX_FIELD_CHARS:
        return text[:MAX_FIELD_CHARS] + "…[truncated]"
    return text


def _base(report_no, queried_at, upstream_code, total) -> dict:
    return {
        "schema_version": 1,
        "status": "error",
        "error": None,
        "message": "",
        "report_no": report_no,
        "match": "exact",
        "queried_at": queried_at,
        "upstream_code": upstream_code,
        "upstream_total_count": total,
        "filings": [],
        "missing_fields": [],
        "truncated": False,
        "price": None,
        "purchase_url": None,
        "not_in_source": ["price", "purchase_url"],
        "disclaimer": DISCLAIMER,
        "source": dict(SOURCE),
    }


def _fit(payload: dict) -> dict:
    if len(json.dumps(payload, ensure_ascii=False)) <= 20_000:
        return payload
    for filing in payload["filings"]:
        for key, value in filing.items():
            if isinstance(value, str) and len(value) > 180:
                filing[key] = value[:180] + "…[truncated]"
    if len(json.dumps(payload, ensure_ascii=False)) > 20_000:
        payload["filings"] = payload["filings"][:1]
        payload["truncated"] = True
    return payload


def _redact(payload: dict, secret: str) -> dict:
    text = _redact_text(json.dumps(payload, ensure_ascii=False), secret)
    return json.loads(text)


def _redact_text(text: str, secret: str) -> str:
    if secret and len(secret) >= 8:
        text = text.replace(secret, "[redacted]")
    return _API_URL_RE.sub("[official-api]", text)
