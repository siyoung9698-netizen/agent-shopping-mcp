"""I0030 filing lookup.

Responses below are fixtures. They are not live FoodSafetyKorea results.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import timedelta

from mcp.shared.memory import create_connected_server_and_client_session

import functional_food
import metering
import server

# Fixture marker: synthetic rows shaped like I0030, not a captured live filing.
FIXTURE = True
FIXTURE_KEY = "fixture-key-0123456789"
REQUIRED_FILING_KEYS = (
    "report_no",
    "product_name",
    "company_name",
    "license_no",
    "permitted_on",
    "last_updated",
    "shelf_life",
    "appearance",
    "form",
    "intake",
    "filed_claim_text",
    "cautions",
    "storage",
    "product_type",
    "specification",
    "functional_ingredients",
    "other_ingredients",
    "capsule_ingredients",
    "indicator_ingredients",
    "production_ended",
    "high_calorie_low_nutrient",
    "child_preference_certified",
    "packaging_material",
    "packaging_method",
    "business_type",
)


def fixture_row(report_no="100000000001", **overrides):
    row = {
        "PRDLST_REPORT_NO": report_no,
        "PRDLST_NM": "FIXTURE-PRODUCT",
        "BSSH_NM": "FIXTURE-COMPANY",
        "LCNS_NO": "10000000000",
        "PRMS_DT": "20200101",
        "LAST_UPDT_DTM": "20240101",
        "POG_DAYCNT": "24개월",
        "DISPOS": "정제",
        "PRDT_SHAP_CD_NM": "정",
        "NTK_MTHD": "1일 1회",
        "PRIMARY_FNCLTY": "FIXTURE 기능성 문구",
        "IFTKN_ATNT_MATR_CN": "FIXTURE 주의",
        "CSTDY_MTHD": "",
        "PRDLST_CDNM": "비타민",
        "STDR_STND": "FIXTURE 규격",
        "INDIV_RAWMTRL_NM": "FIXTURE 원료",
        "ETC_RAWMTRL_NM": "",
        "CAP_RAWMTRL_NM": None,
        "RAWMTRL_NM": "비타민 C",
        "PRODUCTION": "아니오",
        "HIENG_LNTRT_DVS_NM": "해당없음",
        "CHILD_CRTFC_YN": "",
        "FRMLC_MTRQLT": "PE",
        "FRMLC_MTHD": "병",
        "INDUTY_CD_NM": "건강기능식품전문제조업",
    }
    row.update(overrides)
    return row


def fixture_body(rows, code="INFO-000", total=None):
    if isinstance(rows, dict):
        rows = [rows]
    if total is None:
        total = len(rows)
    return {
        "I0030": {
            "total_count": str(total),
            "row": rows,
            "RESULT": {"CODE": code, "MSG": "fixture"},
        }
    }


def assert_public_payload(test, payload, secret=FIXTURE_KEY):
    text = json.dumps(payload, ensure_ascii=False)
    test.assertNotIn(secret, text)
    test.assertNotIn("openapi.foodsafetykorea.go.kr", text)
    test.assertIsNone(payload["price"])
    test.assertIsNone(payload["purchase_url"])
    test.assertEqual(payload["not_in_source"], ["price", "purchase_url"])
    test.assertIn("보증", payload["disclaimer"])
    test.assertEqual(payload["source"]["service_id"], "I0030")
    test.assertEqual(payload["source"]["documentation_url"], functional_food.DOC_URL)
    test.assertEqual(payload["source"]["dataset_url"], functional_food.DATASET_URL)
    test.assertEqual(payload["match"], "exact")


class SchemaTests(unittest.TestCase):
    def test_exact_fixture_has_filing_fields_and_explicit_gaps(self):
        payload = functional_food.interpret_response(
            fixture_body(fixture_row()),
            "100000000001",
            "2026-11-08T00:00:00Z",
        )
        assert_public_payload(self, payload)
        self.assertEqual(payload["status"], "ok")
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["report_no"], "100000000001")
        self.assertEqual(payload["queried_at"], "2026-11-08T00:00:00Z")
        self.assertEqual(payload["upstream_code"], "INFO-000")
        self.assertEqual(len(payload["filings"]), 1)
        filing = payload["filings"][0]
        self.assertEqual(set(filing), set(REQUIRED_FILING_KEYS))
        self.assertEqual(filing["report_no"], "100000000001")
        self.assertEqual(filing["intake"], "1일 1회")
        self.assertEqual(filing["cautions"], "FIXTURE 주의")
        self.assertEqual(filing["functional_ingredients"], "FIXTURE 원료")
        self.assertIsNone(filing["storage"])
        self.assertIn("storage", payload["missing_fields"])
        self.assertIn("capsule_ingredients", payload["missing_fields"])
        self.assertIn("child_preference_certified", payload["missing_fields"])
        self.assertNotIn("intake", payload["missing_fields"])
        self.assertNotIn("price", filing)

    def test_exact_mismatch_no_result_and_leading_zero_do_not_return_another_product(self):
        other = functional_food.interpret_response(
            fixture_body(fixture_row("200000000002", PRDLST_NM="FIXTURE-OTHER")),
            "100000000001",
            "2026-11-08T00:00:00Z",
        )
        self.assertEqual(other["status"], "no_exact_match")
        self.assertEqual(other["filings"], [])
        self.assertNotIn("FIXTURE-OTHER", json.dumps(other, ensure_ascii=False))

        missing = functional_food.interpret_response(
            fixture_body([], code="INFO-200", total=0),
            "100000000001",
            "2026-11-08T00:00:00Z",
        )
        self.assertEqual(missing["status"], "no_exact_match")
        self.assertEqual(missing["upstream_code"], "INFO-200")

        padded = functional_food.interpret_response(
            fixture_body(fixture_row("100000000001")),
            "0100000000001",
            "2026-11-08T00:00:00Z",
        )
        self.assertEqual(padded["status"], "no_exact_match")
        self.assertEqual(padded["report_no"], "0100000000001")
        self.assertEqual(padded["filings"], [])

    def test_multiple_rows_and_incomplete_page_are_not_collapsed(self):
        first = fixture_row("100000000001", PRDLST_NM="FIXTURE-A")
        second = fixture_row("100000000001", PRDLST_NM="FIXTURE-B")
        many = functional_food.interpret_response(
            fixture_body([first, second], total=2),
            "100000000001",
            "2026-11-08T00:00:00Z",
        )
        self.assertEqual(many["status"], "multiple")
        self.assertEqual([row["product_name"] for row in many["filings"]], ["FIXTURE-A", "FIXTURE-B"])

        inconclusive = functional_food.interpret_response(
            fixture_body(fixture_row("999"), total=9),
            "100000000001",
            "2026-11-08T00:00:00Z",
        )
        self.assertEqual(inconclusive["status"], "inconclusive")
        self.assertEqual(inconclusive["filings"], [])
        self.assertNotIn("FIXTURE-PRODUCT", json.dumps(inconclusive, ensure_ascii=False))

    def test_identifier_rejects_names_and_keeps_leading_zeroes_in_the_request(self):
        with self.assertRaises(functional_food.FilingError) as caught:
            functional_food.validate_report_no("성윤")
        self.assertEqual(caught.exception.code, "invalid_identifier")
        kept = functional_food.validate_report_no(" 00123 ")
        self.assertEqual(kept, "00123")
        url = functional_food.build_request_url("00123", FIXTURE_KEY)
        self.assertTrue(url.startswith("https://openapi.foodsafetykorea.go.kr/api/"))
        self.assertIn("/I0030/json/1/5/PRDLST_REPORT_NO=00123", url)
        self.assertNotIn("BSSH_NM", url)
        self.assertNotIn("PRDLST_NM", url)
        functional_food.ensure_official_url(url)
        with self.assertRaises(functional_food.FilingError):
            functional_food.ensure_official_url("https://example.com/api/x/I0030/json/1/5/PRDLST_REPORT_NO=00123")
        with self.assertRaises(functional_food.FilingError):
            functional_food.ensure_official_url(
                "http://openapi.foodsafetykorea.go.kr/api/x/I0030/json/1/5/PRDLST_REPORT_NO=00123"
            )

    def test_redirect_off_host_does_not_include_the_target(self):
        handler = functional_food.OfficialRedirectHandler()
        request = urllib.request.Request(
            "https://openapi.foodsafetykorea.go.kr/api/fixture-key-0123456789/I0030/json/1/5/PRDLST_REPORT_NO=1"
        )
        with self.assertRaises(urllib.error.URLError) as caught:
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/steal")
        self.assertNotIn("evil.example", str(caught.exception))
        self.assertNotIn(FIXTURE_KEY, str(caught.exception))


class _Body:
    def __init__(self, payload, url):
        self._data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self._url = url
        self._off = 0

    def read(self, size=8192):
        chunk = self._data[self._off : self._off + size]
        self._off += size
        return chunk

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class TransportTests(unittest.TestCase):
    def _open(self, behavior):
        def opener(request, timeout=None):
            opener.urls.append(request.full_url)
            opener.timeouts.append(timeout)
            result = behavior(request)
            if isinstance(result, Exception):
                raise result
            return result

        opener.urls = []
        opener.timeouts = []
        return opener

    def test_timeout_upstream_failure_auth_and_quota_hide_the_request_url(self):
        timeout = self._open(lambda _request: TimeoutError("timed out"))
        with self.assertRaises(functional_food.FilingError) as timed:
            functional_food.fetch_i0030("100000000001", FIXTURE_KEY, opener=timeout)
        self.assertEqual(timed.exception.code, "upstream_timeout")
        self.assertNotIn(FIXTURE_KEY, timed.exception.message)
        self.assertNotIn("openapi.foodsafetykorea.go.kr", timed.exception.message)
        self.assertEqual(timeout.timeouts, [functional_food.TIMEOUT_SECONDS])
        self.assertIn("/PRDLST_REPORT_NO=100000000001", timeout.urls[0])

        failed = self._open(lambda request: urllib.error.URLError(f"boom {request.full_url}"))
        with self.assertRaises(functional_food.FilingError) as upstream:
            functional_food.fetch_i0030("100000000001", FIXTURE_KEY, opener=failed)
        self.assertEqual(upstream.exception.code, "upstream_error")
        self.assertNotIn(FIXTURE_KEY, upstream.exception.message)
        self.assertNotIn("openapi.foodsafetykorea.go.kr", upstream.exception.message)

        def auth(_request):
            return _Body(
                {
                    "I0030": {
                        "RESULT": {
                            "CODE": "INFO-100",
                            "MSG": f"bad https://openapi.foodsafetykorea.go.kr/api/{FIXTURE_KEY}/I0030 {FIXTURE_KEY}",
                        }
                    }
                },
                f"https://openapi.foodsafetykorea.go.kr/api/{FIXTURE_KEY}/I0030/json/1/5/PRDLST_REPORT_NO=100000000001",
            )

        with self.assertRaises(functional_food.FilingError) as denied:
            functional_food.fetch_i0030("100000000001", FIXTURE_KEY, opener=self._open(auth))
        self.assertEqual(denied.exception.code, "upstream_auth")
        self.assertEqual(denied.exception.extra["upstream_code"], "INFO-100")
        self.assertNotIn(FIXTURE_KEY, denied.exception.message)

        def quota(_request):
            return _Body({"I0030": {"RESULT": {"CODE": "INFO-300", "MSG": "quota"}}}, _request_url())

        with self.assertRaises(functional_food.FilingError) as limited:
            functional_food.fetch_i0030("100000000001", FIXTURE_KEY, opener=self._open(quota))
        self.assertEqual(limited.exception.code, "upstream_quota")
        self.assertEqual(limited.exception.extra["upstream_code"], "INFO-300")

        def huge(_request):
            return _Body(b"x" * (functional_food.MAX_RESPONSE_BYTES + 10), _request_url())

        with self.assertRaises(functional_food.FilingError) as oversized:
            functional_food.fetch_i0030("100000000001", FIXTURE_KEY, opener=self._open(huge))
        self.assertEqual(oversized.exception.code, "upstream_error")
        self.assertIn("허용 크기", oversized.exception.message)

    def test_successful_fixture_response_redacts_the_key_and_api_url(self):
        def ok(request):
            row = fixture_row()
            row["CSTDY_MTHD"] = f"see https://openapi.foodsafetykorea.go.kr/api/{FIXTURE_KEY}/I0030"
            return _Body(fixture_body(row), request.full_url)

        payload = functional_food.fetch_i0030("100000000001", FIXTURE_KEY, opener=self._open(ok))
        assert_public_payload(self, payload)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("[official-api]", payload["filings"][0]["storage"])
        self.assertNotIn(FIXTURE_KEY, payload["filings"][0]["storage"])


def _request_url():
    return (
        "https://openapi.foodsafetykorea.go.kr/api/"
        f"{FIXTURE_KEY}/I0030/json/1/5/PRDLST_REPORT_NO=100000000001"
    )


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MCP_API_KEYS"] = "dev-demo-key"
        os.environ["MCP_MONTHLY_QUOTA"] = "100"
        os.environ["MCP_API_KEY"] = "dev-demo-key"
        os.environ["MCP_USAGE_DB_PATH"] = os.path.join(self.tmp.name, "usage.db")
        os.environ.pop("FOODSAFETYKOREA_API_KEY", None)
        metering.reset_meter()
        self._original_fetch = functional_food.fetch_i0030

    async def asyncTearDown(self):
        functional_food.fetch_i0030 = self._original_fetch
        self.tmp.cleanup()

    def _session(self):
        return create_connected_server_and_client_session(
            server.mcp,
            read_timeout_seconds=timedelta(seconds=10),
            raise_exceptions=True,
        )

    async def test_unconfigured_and_invalid_id_do_not_call_the_network_or_invent_a_filing(self):
        calls = []

        def forbidden(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("network fetch should not run")

        functional_food.fetch_i0030 = forbidden
        async with self._session() as client:
            missing = await client.call_tool("lookup_functional_food_filing", {"report_no": "100000000001"})
            self.assertTrue(missing.isError, missing)
            body = json.loads(missing.content[0].text)
            self.assertEqual(body["error"], "filing_api_unconfigured")
            self.assertEqual(body["filings"], [])
            self.assertIsNone(body["price"])
            self.assertIn("FOODSAFETYKOREA_API_KEY", body["message"])
            self.assertNotIn("FIXTURE-PRODUCT", missing.content[0].text)

            rejected = await client.call_tool("lookup_functional_food_filing", {"report_no": "프로메가"})
            self.assertTrue(rejected.isError, rejected)
            rejected_body = json.loads(rejected.content[0].text)
            self.assertEqual(rejected_body["error"], "invalid_identifier")
            self.assertEqual(rejected_body["filings"], [])

            usage = json.loads((await client.call_tool("get_usage", {})).content[0].text)
        self.assertEqual(calls, [])
        self.assertEqual(usage["calls_used"], 2)
        self.assertIn("lookup_functional_food_filing", usage["metered_tools"])
        self.assertEqual(usage["metered_tools"], list(metering.METERED_TOOLS))
        self.assertNotIn("get_usage", usage["metered_tools"])

    async def test_fixture_success_quota_and_upstream_errors_are_tool_results(self):
        os.environ["FOODSAFETYKOREA_API_KEY"] = FIXTURE_KEY
        calls = []

        def fake(report_no, api_key, **_kwargs):
            calls.append((report_no, api_key))
            if fake.mode == "ok":
                return functional_food.interpret_response(
                    fixture_body(fixture_row(report_no)),
                    report_no,
                    "2026-11-08T01:02:03Z",
                )
            if fake.mode == "timeout":
                raise functional_food.FilingError("upstream_timeout", "공식 API 응답이 제한 시간 안에 오지 않았다.")
            if fake.mode == "auth":
                raise functional_food.FilingError("upstream_auth", "공식 API가 인증키 또는 활용신청을 거부했다.", upstream_code="INFO-100")
            raise functional_food.FilingError("upstream_quota", "공식 API 호출 한도에 걸렸다.", upstream_code="INFO-300")

        fake.mode = "ok"
        functional_food.fetch_i0030 = fake
        async with self._session() as client:
            found = await client.call_tool("lookup_functional_food_filing", {"report_no": "00100000000001"})
            self.assertFalse(found.isError, found)
            payload = json.loads(found.content[0].text)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["report_no"], "00100000000001")
            self.assertEqual(payload["filings"][0]["report_no"], "00100000000001")
            self.assertIsNone(payload["price"])
            self.assertNotIn(FIXTURE_KEY, found.content[0].text)

            fake.mode = "timeout"
            timed = await client.call_tool("lookup_functional_food_filing", {"report_no": "00100000000001"})
            self.assertTrue(timed.isError, timed)
            self.assertEqual(json.loads(timed.content[0].text)["error"], "upstream_timeout")

            fake.mode = "auth"
            denied = await client.call_tool("lookup_functional_food_filing", {"report_no": "00100000000001"})
            self.assertEqual(json.loads(denied.content[0].text)["error"], "upstream_auth")
            self.assertEqual(json.loads(denied.content[0].text)["upstream_code"], "INFO-100")

            fake.mode = "quota"
            limited = await client.call_tool("lookup_functional_food_filing", {"report_no": "00100000000001"})
            self.assertEqual(json.loads(limited.content[0].text)["error"], "upstream_quota")

            usage = json.loads((await client.call_tool("get_usage", {})).content[0].text)
        self.assertEqual(usage["calls_used"], 4)
        self.assertEqual([item[0] for item in calls], ["00100000000001"] * 4)
        self.assertTrue(all(item[1] == FIXTURE_KEY for item in calls))

    async def test_mcp_quota_stops_the_filing_tool_before_the_fetch(self):
        os.environ["FOODSAFETYKOREA_API_KEY"] = FIXTURE_KEY
        metering.reset_meter(os.environ["MCP_USAGE_DB_PATH"], {"dev-demo-key": 1})
        calls = []

        def fake(report_no, api_key, **_kwargs):
            calls.append(report_no)
            return functional_food.interpret_response(fixture_body(fixture_row(report_no)), report_no, "2026-11-08T00:00:00Z")

        functional_food.fetch_i0030 = fake
        async with self._session() as client:
            first = await client.call_tool("lookup_functional_food_filing", {"report_no": "100000000001"})
            self.assertFalse(first.isError, first)
            second = await client.call_tool("lookup_functional_food_filing", {"report_no": "100000000001"})
            self.assertTrue(second.isError, second)
            self.assertEqual(json.loads(second.content[0].text)["error"], "quota_exceeded")
        self.assertEqual(calls, ["100000000001"])


if __name__ == "__main__":
    unittest.main()
