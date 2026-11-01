"""API 키 미터링: 유효 키는 카운터가 오르고, 한도 초과는 거절되고, get_usage가 그 수를 반영한다."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult
from starlette.requests import Request

import metering
import server


def _scope(headers: dict[str, str], query: str = "") -> dict:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in headers.items()]
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": query.encode(),
        "headers": raw_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }


@contextmanager
def http_call_context(headers: dict[str, str] | None = None, query: str = ""):
    """Streamable HTTP가 도구 호출에 붙이는 것과 같은 Starlette Request를 컨텍스트에 넣는다."""
    request = Request(_scope(headers or {}, query))
    token = request_ctx.set(
        RequestContext(
            request_id="test",
            meta=None,
            session=None,
            lifespan_context=None,
            request=request,
        )
    )
    try:
        yield
    finally:
        request_ctx.reset(token)


def _error_payload(result: CallToolResult) -> dict:
    assert result.isError, result
    return json.loads(result.content[0].text)


def _price_rows() -> int:
    if not os.path.exists(server.DB_PATH):
        return 0
    con = sqlite3.connect(server.DB_PATH)
    try:
        return con.execute("select count(*) from price_history").fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


class MeterConfigTests(unittest.TestCase):
    def test_per_key_quota_override_and_reject_unknown_key(self):
        keys = metering.parse_key_config(
            "dev-demo-key,partner-key",
            "dev-demo-key:40",
            default_quota=1000,
        )
        self.assertEqual(keys["dev-demo-key"], 40)
        self.assertEqual(keys["partner-key"], 1000)
        with self.assertRaises(ValueError):
            metering.parse_key_config("dev-demo-key", "other:10", 100)

    def test_quota_after_last_colon_keeps_colons_in_the_key(self):
        keys = metering.parse_key_config("ab:cd", "ab:cd:7", 100)
        self.assertEqual(keys, {"ab:cd": 7})

    def test_month_window_resets_on_the_first_utc(self):
        clock = {"now": datetime(2026, 10, 31, 23, 0, tzinfo=timezone.utc)}
        with tempfile.TemporaryDirectory() as tmp:
            meter = metering.UsageMeter(
                os.path.join(tmp, "usage.db"),
                {"dev-demo-key": 10},
                clock=lambda: clock["now"],
            )
            first = meter.consume("dev-demo-key")
            self.assertEqual(first.calls_used, 1)
            self.assertEqual(first.period, "2026-10")
            self.assertEqual(first.resets_at, "2026-11-01T00:00:00Z")
            clock["now"] = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
            rolled = meter.inspect("dev-demo-key")
            self.assertEqual(rolled.calls_used, 0)
            self.assertEqual(rolled.calls_remaining, 10)
            self.assertEqual(rolled.period, "2026-11")

    def test_database_stores_a_hash_not_the_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "usage.db")
            meter = metering.UsageMeter(path, {"dev-demo-key": 5})
            meter.consume("dev-demo-key")
            with open(path, "rb") as fh:
                blob = fh.read()
            self.assertNotIn(b"dev-demo-key", blob)
            con = sqlite3.connect(path)
            try:
                stored = con.execute("select key_id from usage_counters").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(stored, metering.key_fingerprint("dev-demo-key"))


class ToolMeteringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "usage.db")
        os.environ["MCP_API_KEYS"] = "dev-demo-key,partner-key"
        os.environ["MCP_MONTHLY_QUOTA"] = "100"
        os.environ.pop("MCP_KEY_QUOTAS", None)
        os.environ["MCP_API_KEY"] = "dev-demo-key"
        os.environ["MCP_USAGE_DB_PATH"] = self.db_path
        os.environ["NAVER_CLIENT_ID"] = ""
        os.environ["NAVER_CLIENT_SECRET"] = ""
        server.NAVER_ID = ""
        server.NAVER_SECRET = ""
        server.DB_PATH = os.path.join(self.tmp.name, "prices.db")
        metering.reset_meter()

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def _session(self):
        return create_connected_server_and_client_session(
            server.mcp,
            read_timeout_seconds=timedelta(seconds=10),
            raise_exceptions=True,
        )

    async def test_every_tool_lists_full_annotations(self):
        """PlayMCP는 tools/list의 annotations에 title과 네 hint가 모두 있어야 한다."""
        expected = {
            "search_products": {
                "title": "상품 검색",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": True,
            },
            "check_price_timing": {
                "title": "가격 타이밍",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            "get_buy_link": {
                "title": "구매 링크",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            "find_pet_food": {
                "title": "펫사료 검색",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            "compare_nutrition": {
                "title": "영양성분 비교",
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": False,
                "openWorldHint": False,
            },
            "get_usage": {
                "title": "사용량 조회",
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
        }
        async with self._session() as client:
            listed = await client.list_tools()
        by_name = {tool.name: tool for tool in listed.tools}
        self.assertEqual(set(by_name), set(expected))
        for name, want in expected.items():
            annotations = by_name[name].annotations
            self.assertIsNotNone(annotations, name)
            dumped = annotations.model_dump(exclude_none=True)
            self.assertEqual(dumped, want, name)

    async def test_valid_key_increments_and_get_usage_matches(self):
        async with self._session() as client:
            tools = await client.list_tools()
            names = {tool.name for tool in tools.tools}
            self.assertIn("get_usage", names)
            for tool in tools.tools:
                props = tool.inputSchema.get("properties", {})
                self.assertNotIn("ctx", props)
                self.assertNotIn("api_key", props)

            calls = [
                ("search_products", {"keyword": "비타민"}),
                ("check_price_timing", {"product_name": "고려은단 비타민C 1000"}),
                ("get_buy_link", {"product_url": "https://www.coupang.com/"}),
                ("find_pet_food", {"pet": "강아지"}),
                ("compare_nutrition", {"product_a": "로얄캐닌", "product_b": "힐스"}),
            ]
            texts = {}
            for name, args in calls:
                result = await client.call_tool(name, args)
                self.assertFalse(result.isError, result)
                texts[name] = result.content[0].text
            self.assertIn("static_catalog", texts["search_products"])
            self.assertIn("static_catalog", texts["find_pet_food"])
            self.assertIn("영양성분 비교", texts["compare_nutrition"])
            self.assertNotIn("못 찾았어", texts["compare_nutrition"])

            usage = await client.call_tool("get_usage", {})
            self.assertFalse(usage.isError, usage)
            body = json.loads(usage.content[0].text)
            self.assertEqual(body["calls_used"], len(calls))
            self.assertEqual(body["calls_remaining"], 100 - len(calls))
            self.assertEqual(body["quota"], 100)
            self.assertEqual(body["window"], "calendar_month_utc")
            self.assertIn("resets_at", body)
            self.assertEqual(body["metered_tools"], list(metering.METERED_TOOLS))
            self.assertNotIn("dev-demo-key", usage.content[0].text)

            again = await client.call_tool("get_usage", {})
            again_body = json.loads(again.content[0].text)
            self.assertEqual(again_body["calls_used"], len(calls))

    async def test_over_quota_rejects_without_counting_or_running_the_tool(self):
        metering.reset_meter(self.db_path, {"dev-demo-key": 1, "partner-key": 100})
        async with self._session() as client:
            first = await client.call_tool("search_products", {"keyword": "비타민"})
            self.assertFalse(first.isError, first)
            rows_after_first = _price_rows()
            self.assertGreater(rows_after_first, 0)

            denied = await client.call_tool("search_products", {"keyword": "비타민"})
            payload = _error_payload(denied)
            self.assertEqual(payload["error"], "quota_exceeded")
            self.assertEqual(payload["tool"], "search_products")
            self.assertEqual(payload["calls_used"], 1)
            self.assertEqual(payload["calls_remaining"], 0)
            self.assertEqual(payload["quota"], 1)
            self.assertIn("한도", payload["message"])
            self.assertEqual(_price_rows(), rows_after_first)

            usage = json.loads((await client.call_tool("get_usage", {})).content[0].text)
            self.assertEqual(usage["calls_used"], 1)
            self.assertEqual(usage["calls_remaining"], 0)

    async def test_missing_and_invalid_keys_do_not_count(self):
        async with self._session() as client:
            os.environ["MCP_API_KEY"] = ""
            missing = await client.call_tool("search_products", {"keyword": "비타민"})
            self.assertEqual(_error_payload(missing)["error"], "missing_api_key")

            os.environ["MCP_API_KEY"] = "not-a-real-key"
            invalid = await client.call_tool("get_usage", {})
            self.assertEqual(_error_payload(invalid)["error"], "invalid_api_key")

            os.environ["MCP_API_KEY"] = "dev-demo-key"
            usage = json.loads((await client.call_tool("get_usage", {})).content[0].text)
            self.assertEqual(usage["calls_used"], 0)
            self.assertEqual(_price_rows(), 0)

    async def test_keys_do_not_see_each_other(self):
        async with self._session() as client:
            os.environ["MCP_API_KEY"] = "dev-demo-key"
            self.assertFalse((await client.call_tool("search_products", {"keyword": "비타민"})).isError)
            os.environ["MCP_API_KEY"] = "partner-key"
            self.assertFalse((await client.call_tool("find_pet_food", {"pet": "고양이"})).isError)
            self.assertFalse((await client.call_tool("find_pet_food", {"pet": "고양이"})).isError)

            partner = json.loads((await client.call_tool("get_usage", {})).content[0].text)
            os.environ["MCP_API_KEY"] = "dev-demo-key"
            demo = json.loads((await client.call_tool("get_usage", {})).content[0].text)

        self.assertEqual(demo["calls_used"], 1)
        self.assertEqual(partner["calls_used"], 2)
        self.assertNotEqual(demo["key_id"], partner["key_id"])
        self.assertNotIn("partner-key", json.dumps(demo))
        self.assertNotIn("dev-demo-key", json.dumps(partner))

    async def test_http_header_and_query_are_attributed_and_env_key_is_not_a_fallback(self):
        os.environ["MCP_API_KEY"] = "dev-demo-key"
        with http_call_context(headers={}):
            missing = await server.mcp.call_tool("search_products", {"keyword": "비타민"})
        self.assertIsInstance(missing, CallToolResult)
        self.assertEqual(_error_payload(missing)["error"], "missing_api_key")
        self.assertEqual(metering.get_meter().inspect("dev-demo-key").calls_used, 0)

        with http_call_context(headers={"Authorization": "Bearer partner-key"}):
            ok = await server.mcp.call_tool("search_products", {"keyword": "비타민"})
        self.assertNotIsInstance(ok, CallToolResult)
        self.assertEqual(metering.get_meter().inspect("partner-key").calls_used, 1)
        self.assertEqual(metering.get_meter().inspect("dev-demo-key").calls_used, 0)

        with http_call_context(headers={"X-API-Key": "partner-key"}):
            await server.mcp.call_tool("check_price_timing", {"product_name": "없음"})
        self.assertEqual(metering.get_meter().inspect("partner-key").calls_used, 2)

        with http_call_context(query="api_key=dev-demo-key"):
            await server.mcp.call_tool("get_buy_link", {"product_url": "https://example.com"})
        demo = metering.get_meter().inspect("dev-demo-key")
        self.assertEqual(demo.calls_used, 1)

        with http_call_context(
            headers={"Authorization": "Bearer dev-demo-key"},
            query="api_key=partner-key",
        ):
            await server.mcp.call_tool("find_pet_food", {"pet": "강아지"})
        # Authorization이 쿼리보다 우선한다.
        self.assertEqual(metering.get_meter().inspect("dev-demo-key").calls_used, 2)
        self.assertEqual(metering.get_meter().inspect("partner-key").calls_used, 2)

    async def test_streamable_http_client_counts_bearer_and_rejects_missing_key(self):
        """실제 Streamable HTTP 클라이언트가 Authorization 헤더로 호출을 집계한다."""
        os.environ["MCP_API_KEY"] = "should-not-be-used-for-http"
        metering.reset_meter(self.db_path, {"dev-demo-key": 10, "partner-key": 10})
        server.mcp._session_manager = None
        app = server.mcp.streamable_http_app()

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        uv_server = uvicorn.Server(config)
        uv_server.install_signal_handlers = lambda: None
        thread = threading.Thread(target=uv_server.run, daemon=True)
        thread.start()
        try:
            for _ in range(50):
                if uv_server.started:
                    break
                await asyncio.sleep(0.05)
            self.assertTrue(uv_server.started, "streamable http server did not start")
            url = f"http://127.0.0.1:{port}/mcp"

            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer dev-demo-key"},
                timeout=httpx.Timeout(10, read=10),
            ) as http_client:
                async with streamable_http_client(url, http_client=http_client) as (read, write, _sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool("search_products", {"keyword": "비타민"})
                        self.assertFalse(result.isError, result)
                        usage = json.loads((await session.call_tool("get_usage", {})).content[0].text)
                        self.assertEqual(usage["calls_used"], 1)
                        self.assertEqual(usage["calls_remaining"], 9)

            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=10)) as http_client:
                async with streamable_http_client(url, http_client=http_client) as (read, write, _sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        denied = await session.call_tool("search_products", {"keyword": "비타민"})
                        self.assertEqual(_error_payload(denied)["error"], "missing_api_key")

            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=10)) as http_client:
                async with streamable_http_client(
                    f"{url}?api_key=partner-key",
                    http_client=http_client,
                ) as (read, write, _sid):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        queried = await session.call_tool("find_pet_food", {"pet": "강아지"})
                        self.assertFalse(queried.isError, queried)

            self.assertEqual(metering.get_meter().inspect("dev-demo-key").calls_used, 1)
            self.assertEqual(metering.get_meter().inspect("partner-key").calls_used, 1)
        finally:
            uv_server.should_exit = True
            thread.join(timeout=5)

    async def test_unconfigured_server_rejects_tools_with_a_clear_error(self):
        os.environ["MCP_API_KEYS"] = ""
        os.environ["MCP_API_KEY"] = "dev-demo-key"
        metering.reset_meter()
        async with self._session() as client:
            result = await client.call_tool("search_products", {"keyword": "비타민"})
        self.assertEqual(_error_payload(result)["error"], "server_unconfigured")


class HttpRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "usage.db")
        os.environ["MCP_API_KEYS"] = "dev-demo-key,partner-key"
        os.environ["MCP_MONTHLY_QUOTA"] = "100"
        os.environ.pop("MCP_KEY_QUOTAS", None)
        os.environ["MCP_USAGE_DB_PATH"] = self.db_path
        metering.reset_meter()
        # StreamableHTTPSessionManager.run()은 인스턴스당 한 번만 된다.
        server.mcp._session_manager = None
        self.app = server.mcp.streamable_http_app()

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_is_public_and_usage_is_scoped_to_the_presented_key(self):
        from starlette.testclient import TestClient

        meter = metering.get_meter()
        meter.consume("dev-demo-key")
        meter.consume("partner-key")
        meter.consume("partner-key")

        with TestClient(self.app) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")
            self.assertNotIn("calls_used", health.text)
            self.assertNotIn("dev-demo-key", health.text)
            self.assertNotIn("partner-key", health.text)

            missing = client.get("/usage")
            self.assertEqual(missing.status_code, 401)
            self.assertEqual(missing.json()["error"], "missing_api_key")

            demo = client.get("/usage", headers={"Authorization": "Bearer dev-demo-key"})
            self.assertEqual(demo.status_code, 200)
            self.assertEqual(demo.json()["calls_used"], 1)
            self.assertEqual(demo.json()["calls_remaining"], 99)
            self.assertNotIn("partner-key", demo.text)

            partner = client.get("/usage", headers={"X-API-Key": "partner-key"})
            self.assertEqual(partner.json()["calls_used"], 2)
            self.assertNotEqual(partner.json()["key_id"], demo.json()["key_id"])

            queried = client.get("/usage?api_key=dev-demo-key")
            self.assertEqual(queried.json()["calls_used"], 1)

            # 헤더가 있으면 쿼리의 다른 키는 무시한다.
            preferred = client.get(
                "/usage?api_key=partner-key",
                headers={"Authorization": "Bearer dev-demo-key"},
            )
            self.assertEqual(preferred.json()["key_id"], demo.json()["key_id"])

            invalid = client.get("/usage", headers={"Authorization": "Bearer nope"})
            self.assertEqual(invalid.status_code, 401)
            self.assertEqual(invalid.json()["error"], "invalid_api_key")
            self.assertNotIn("calls_used", invalid.text)

            # 조회는 카운터를 올리지 않는다.
            self.assertEqual(meter.inspect("dev-demo-key").calls_used, 1)
            self.assertEqual(meter.inspect("partner-key").calls_used, 2)

    def test_usage_reports_unconfigured_server(self):
        from starlette.testclient import TestClient

        os.environ["MCP_API_KEYS"] = ""
        metering.reset_meter()
        with TestClient(self.app) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            usage = client.get("/usage", headers={"X-API-Key": "dev-demo-key"})
        self.assertEqual(usage.status_code, 503)
        self.assertEqual(usage.json()["error"], "server_unconfigured")


if __name__ == "__main__":
    unittest.main()
